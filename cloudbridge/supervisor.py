"""jax-voice-bridge：把 PC 端的 sidecar 与 rtc_bridge 合并为一个云端容器进程。

为什么存在
----------
过去这两个进程跑在用户 Windows 机器上，靠本地守护与计划任务维持存活，另有一堆
本地脚本站兜底。用户已明确要求**消灭一切本地运行态与脚本**，因此把它们原样搬进
同一个容器：

    ┌ 本容器 ────────────────────────────────────────────────┐
    │  rtc_bridge (python)  127.0.0.1:19092 WS / 19093 health │
    │        ▲                                                │
    │        │ localhost WS（协议零改动）                     │
    │  sidecar (electron + TRTC SDK, xvfb 无头)               │
    │        │                                                │
    │        └── HTTPS ──▶ 云端控制面 jax-voice-api           │
    │  supervisor（本文件）：拉起两者 + 自证状态 + 退出即重启 │
    └─────────────────────────────────────────────────────────┘

三条硬性约定
------------
1. **子进程是唯一真相**：任一子进程退出 → 本进程立即以非零码退出，交由平台重启。
   这就是用平台健康检查替代本地 watchdog，不做任何自愈脚本。
2. **状态由产品自己报**：`GET /api/v1/voice/bridge/status` 返回子进程存活、
   rtc_bridge 健康探测、sidecar 版本、控制面地址等，供部署门禁判断，不靠人看日志。
3. **凭据只来自环境变量**：容器内不落任何明文凭据文件，也不读取本地配置文件。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jax-voice-bridge")

SERVER_ROOT = Path(__file__).resolve().parent.parent  # 容器内 = /srv
BACKEND_DIR = SERVER_ROOT / "backend"
SIDECAR_DIR = SERVER_ROOT / "sidecar"

START_MONO = time.monotonic()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Child:
    """被监督的子进程：记录启动参数、退出码、重启次数，并保留输出尾部。

    为什么要保留输出尾部：容器崩溃后由平台重启，**上一次的 stdout 会随容器消失**
    且该服务的输出不进入可检索日志（实测 CLS 只有网关访问日志）。若不在进程内留住
    证据，就只能靠猜。状态端点会把这些尾部回报出来——崩溃自我解释，不依赖平台日志。
    """

    TAIL_LINES = 40

    def __init__(self, name: str, argv: list[str], cwd: Path, extra_env: dict[str, str]) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.exit_code: int | None = None
        self.starts = 0
        self.tail: list[str] = []
        self._lock = threading.Lock()
        env = dict(os.environ)
        env.update(extra_env)
        self._env = env
        self.proc: subprocess.Popen | None = None

    def _pump(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                with self._lock:
                    self.tail.append(line)
                    if len(self.tail) > self.TAIL_LINES:
                        del self.tail[: len(self.tail) - self.TAIL_LINES]
                logger.info("[%s] %s", self.name, line)
        except Exception:  # 读管道失败不应影响监督逻辑
            return

    def start(self) -> None:
        logger.info("starting %s: %s (cwd=%s)", self.name, " ".join(self.argv), self.cwd)
        self.proc = subprocess.Popen(  # noqa: S603 - argv 由本文件构造，非外部输入
            self.argv, cwd=str(self.cwd), env=self._env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.starts += 1
        threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True).start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def reap(self) -> int | None:
        """子进程已退出则记录退出码并返回它，否则返回 None。"""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code is not None and self.exit_code is None:
            self.exit_code = code
            logger.error("%s exited with code %s", self.name, code)
        return code

    def signal(self, sig: int) -> None:
        if self.alive() and self.proc is not None:
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def describe(self) -> dict:
        with self._lock:
            tail = list(self.tail)
        return {
            "alive": self.alive(),
            "pid": None if self.proc is None else self.proc.pid,
            "starts": self.starts,
            "exit_code": self.exit_code,
            "output_tail": tail,
        }


class BridgeSupervisor:
    """拉起 rtc_bridge + sidecar，并对外暴露自证状态。"""

    def __init__(self) -> None:
        self.bridge_ws = _env("BRIDGE_WS_URL", "ws://127.0.0.1:19092")
        self.bridge_health_url = _env("BRIDGE_HEALTH_URL", "http://127.0.0.1:19093/health")
        self.sign_url = _env("CONTROL_PLANE_BASE_URL", "")
        self.device_id = _env("BRIDGE_DEVICE_ID", "jax-cloud-bridge")
        self.sidecar_enabled = _env("BRIDGE_SIDECAR_ENABLED", "true").lower() not in {"0", "false", "no"}
        # 子进程死亡后保持状态端点存活的秒数（0 = 立即退出）。
        # 平台不收集容器 stdout，宽限期是唯一能让死因被外部读到的窗口。
        try:
            self.crash_grace_s = float(_env("BRIDGE_CRASH_GRACE_S", "120"))
        except ValueError:
            self.crash_grace_s = 120.0
        self.shutting_down = False

        self.bridge = Child(
            "rtc_bridge",
            [sys.executable, "-m", "rtc_bridge.main"],
            BACKEND_DIR,
            {"PYTHONPATH": str(BACKEND_DIR), "PYTHONUNBUFFERED": "1"},
        )
        # Electron 需要 X display：容器内用 xvfb 提供虚拟显示，无头运行。
        # Chromium 开关是必须的（实测缺 --no-sandbox 时直接 SIGTRAP 退出）：
        #   --no-sandbox            容器内以 root 运行时 Chromium 拒绝启动
        #   --disable-gpu           容器无 GPU，避免 GL 初始化失败
        #   --disable-dev-shm-usage CloudRun 的 /dev/shm 很小，避免渲染进程崩溃
        self.sidecar = Child(
            "sidecar",
            [
                "xvfb-run", "-a",
                str(SIDECAR_DIR / "node_modules" / ".bin" / "electron"),
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                ".",
                "--role=sidecar",
                f"--bridge-url={self.bridge_ws}",
                f"--sign-url={self.sign_url}",
                f"--device={self.device_id}",
            ],
            SIDECAR_DIR,
            {},
        )

    # ---- 生命周期 ----

    def start(self) -> None:
        self.bridge.start()
        if self.sidecar_enabled:
            self.sidecar.start()
        else:
            logger.warning("sidecar disabled by BRIDGE_SIDECAR_ENABLED")

    def terminate_all(self) -> None:
        self.shutting_down = True
        for child in (self.sidecar, self.bridge):
            child.signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        for child in (self.sidecar, self.bridge):
            while child.alive() and time.monotonic() < deadline:
                time.sleep(0.2)
            child.signal(signal.SIGKILL)

    def watch(self) -> None:
        """任一子进程退出即进入宽限期，然后以非零码退出（由平台重启）。

        为什么要宽限期：平台只把网关访问日志收进可检索日志，**容器 stdout 查不到**，
        而崩溃后容器立刻重启会让死因彻底消失。用一段宽限期保持状态端点存活，运维与
        部署门禁就能从 /api/v1/voice/bridge/status 读到 exit_code 与 output_tail。
        宽限期结束后仍然退出，绝不做本地自愈。
        """
        while not self.shutting_down:
            dead = self._first_dead()
            if dead is None:
                time.sleep(1.0)
                continue
            grace = self.crash_grace_s
            logger.error(
                "%s died (exit=%s); holding the status endpoint for %ss before exiting",
                dead.name, dead.exit_code, grace,
            )
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and not self.shutting_down:
                time.sleep(1.0)
            raise SystemExit(1)

    def _first_dead(self) -> Child | None:
        if self.bridge.reap() is not None:
            return self.bridge
        if self.sidecar_enabled and self.sidecar.reap() is not None:
            return self.sidecar
        return None

    # ---- 自证状态 ----

    def _probe_bridge_health(self) -> str:
        try:
            with urllib.request.urlopen(self.bridge_health_url, timeout=2) as resp:
                return "ok" if resp.status == 200 else f"http_{resp.status}"
        except Exception as exc:  # 探测失败只报类型，不阻断状态端点
            return type(exc).__name__

    def sdk_version(self) -> str:
        """sidecar 依赖里声明的 TRTC SDK 版本（不在运行期猜）。"""
        try:
            pkg = json.loads((SIDECAR_DIR / "package.json").read_text(encoding="utf-8"))
            return str(pkg.get("dependencies", {}).get("trtc-electron-sdk", ""))
        except Exception:
            return ""

    def status(self) -> dict:
        bridge_health = self._probe_bridge_health()
        payload = {
            "service": "jax-voice-bridge",
            "uptime_s": round(time.monotonic() - START_MONO, 1),
            "sidecar_enabled": self.sidecar_enabled,
            "rtc_bridge": self.bridge.describe(),
            "sidecar": self.sidecar.describe(),
            "rtc_bridge_health": bridge_health,
            "sign_url": self.sign_url,
            "device_id": self.device_id,
            "trtc_sdk_version": self.sdk_version(),
        }
        payload["ok"] = bool(
            self.bridge.alive()
            and bridge_health == "ok"
            and (self.sidecar.alive() if self.sidecar_enabled else True)
        )
        return payload


class _Handler(BaseHTTPRequestHandler):
    supervisor: BridgeSupervisor

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path in ("/health", "/healthz"):
            self._send(200 if self.supervisor.bridge.alive() else 503,
                       {"status": "ok" if self.supervisor.bridge.alive() else "unavailable"})
            return
        if self.path == "/api/v1/voice/bridge/status":
            payload = self.supervisor.status()
            self._send(200 if payload["ok"] else 503, payload)
            return
        self._send(404, {"code": 40400, "message": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # 降低噪声
        logger.info("http %s", fmt % args)


def main() -> int:
    port = int(_env("PORT", "9200"))
    supervisor = BridgeSupervisor()
    _Handler.supervisor = supervisor
    supervisor.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("bridge status server listening on 0.0.0.0:%s", port)

    def _shutdown(signum, _frame) -> None:
        logger.info("received signal %s, shutting down children", signum)
        supervisor.terminate_all()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        supervisor.watch()
    finally:
        supervisor.terminate_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
