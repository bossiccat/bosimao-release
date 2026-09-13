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
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# supervisor.py 以脚本方式启动（python cloudbridge/supervisor.py）；显式把本目录放入
# sys.path，使 `import sim_phone` 在"脚本运行"与"被测试 importlib 加载"两种方式下都成立。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_phone  # noqa: E402
import sim_provision  # noqa: E402
import tls_material  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jax-voice-bridge")

SERVER_ROOT = Path(__file__).resolve().parent.parent  # 容器内 = /srv
BACKEND_DIR = SERVER_ROOT / "backend"
SIDECAR_DIR = SERVER_ROOT / "sidecar"

START_MONO = time.monotonic()

# 手机模拟器「专有」日志文件名——解析指标时**只认这两个**。
# 绝不按 *.log 通配读整个目录：容器里 sidecar（同容器另一子进程）也会写
# `sidecar-sidecar.log` / 同名 `sidecar-main-diag.log`，混读会解析出**假的成功指标**
# （实测：在开发机上直接读 sidecar/logs 的历史日志，得出了「replied / reply 103 帧」
# 这种与本次运行无关的结论——比看不见日志更危险）。
_SIM_LOG_FILES = ("sidecar-phone.log", "sidecar-main-diag.log")

# 子进程输出里的纯噪声：Chromium 无 D-Bus 总线时的连接错误，条数极多且从不携带
# 有效信息（容器里没有 system bus）。不过滤掉会把真正的死因挤出尾部窗口。
_NOISE_RE = re.compile(r"bus\.cc\(\d+\)|Failed to connect to the bus")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Child:
    """被监督的子进程：记录启动参数、退出码、重启次数，并保留输出尾部。

    为什么要保留输出尾部：容器崩溃后由平台重启，**上一次的 stdout 会随容器消失**
    且该服务的输出不进入可检索日志（实测 CLS 只有网关访问日志）。若不在进程内留住
    证据，就只能靠猜。状态端点会把这些尾部回报出来——崩溃自我解释，不依赖平台日志。
    """

    TAIL_LINES = 120

    def __init__(self, name: str, argv: list[str], cwd: Path, extra_env: dict[str, str],
                 *, liveness: bool = True) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        # liveness=False 的子进程是"一次性任务"（如手机模拟器）：它跑完就退出是**预期行为**，
        # 不得据此判定整体失败并触发容器重启。
        self.liveness = liveness
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
                # Chromium 在容器里会刷大量 dbus 噪声（无 D-Bus 总线），几条就把真正
                # 的信号挤出尾部窗口——实测正是它让「渲染器没起来」看起来毫无输出。
                if _NOISE_RE.search(line):
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

        # 云端手机模拟（不依赖真机）：用真实 TRTC 链路跑一次完整语音往返并量化。
        # 默认关闭；开启后由本进程在容器内生成中文提示音、拉起 phone 角色 Electron。
        self.sim_enabled = _env("BRIDGE_SIM_PHONE", "false").lower() in {"1", "true", "yes"}
        self.sim_device_id = _env("SIM_DEVICE_ID", "jax-sim-phone")
        # 设备凭证：显式 token 优先（复用已注册设备，不发 provisioning）；否则用 owner
        # 凭证走一遍真实配对。两者都缺时由 sim_provision 报 config 阶段失败。
        self.sim_owner_credential = _env("SIM_OWNER_CREDENTIAL", "")
        self.sim_device_credential = _env("SIM_DEVICE_CREDENTIAL", "")
        self.sim_device_name = _env("SIM_DEVICE_NAME", "jax-sim-phone")
        self.sim_join_grace_s = int(_env("SIM_JOIN_GRACE_S", "8") or 8)
        self.sim_prompt_wav = Path(_env("SIM_PROMPT_WAV", "/srv/sim/prompt.wav"))
        self.sim_out_wav = Path(_env("SIM_OUT_WAV", "/srv/sim/reply.wav"))
        # 渲染进程在无头环境里 stdout 不可靠（见 sidecar/logger.js），日志写文件；
        # 指定目录后由 status() 回读，否则模拟器死因只能靠猜。
        self.sim_log_dir = Path(_env("SIM_LOG_DIR", "/tmp/sim-logs"))
        self.sim_hold_s = int(_env("SIM_HOLD_S", "45") or 45)
        self.sim_prompt_text = _env("SIM_PROMPT_TEXT", "")
        self.sim_phone: Child | None = None
        self.sim_metrics = sim_phone.PhoneSimMetrics()
        self._sim_lock = threading.Lock()

        # 子进程 env：*_FILE 指向的文件在 start() 时由注入的 PEM 环境变量落盘
        # （见 tls_material）。私钥绝不烘进镜像。
        # CONTROL_PLANE_BASE_URL 是服务既有键名，而 rtc_bridge 读的是
        # RTC_BRIDGE_CONTROL_PLANE_BASE_URL；后者未显式设置时做一次别名，
        # 避免「配了却没生效」的静默失配。
        bridge_extra_env = {"PYTHONPATH": str(BACKEND_DIR), "PYTHONUNBUFFERED": "1"}
        if not _env("RTC_BRIDGE_CONTROL_PLANE_BASE_URL") and self.sign_url:
            bridge_extra_env["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"] = self.sign_url
        self._bridge_extra_env = bridge_extra_env
        self._tls_material = {"ok": True, "error": ""}
        self.bridge = Child(
            "rtc_bridge",
            [sys.executable, "-m", "rtc_bridge.main"],
            BACKEND_DIR,
            bridge_extra_env,
        )
        # Electron 需要 X display：容器内用 xvfb 提供虚拟显示，无头运行。
        # Chromium 开关是必须的（实测缺 --no-sandbox 时直接 SIGTRAP 退出）：
        #   --no-sandbox            容器内以 root 运行时 Chromium 拒绝启动
        #   --disable-gpu           容器无 GPU，避免 GL 初始化失败
        #   --disable-dev-shm-usage CloudRun 的 /dev/shm 很小，避免渲染进程崩溃
        #
        # ⚠️ 不要传 --device：sidecar 的启动校验按角色 fail-closed，
        #    `role=sidecar` 带 --device 会被判 SIDECAR_UNEXPECTED_DEVICE_ARG 并退出
        #    （config.js:64 / rtc.js:364；--device 只属于 role=phone）。
        #    实测：容器因此崩溃重启，且该错误只能靠产品自报才看到。
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
            ],
            SIDECAR_DIR,
            {},
        )

    # ---- 生命周期 ----

    def _materialize_bridge_tls(self) -> None:
        """把 PEM 形式的证书材料落成受限临时文件，并把 *_FILE 注入 bridge 子进程。

        私钥绝不烘进镜像（镜像层可被任意拉取者读到）：这里落盘的是注入到进程环境
        的 PEM 文本，目录 0o700、文件 0o600。三个 PEM 全空（未配置）时保持现状不动，
        由 rtc_bridge 既有的 fail-closed 语义处理。失败**不静默吞掉**——写入 status()
        供部署门禁读到死因，与「崩溃自我解释」的既有做法一致。
        """
        target_dir = Path(_env("RTC_BRIDGE_TLS_DIR") or "/tmp/jax-voice-bridge-tls")
        try:
            files = tls_material.materialize_tls_files(target_dir, dict(os.environ))
        except tls_material.TlsMaterialError as exc:
            # 异常文本只含缺失键名/目录，绝不含 PEM 内容。
            self._tls_material = {"ok": False, "error": str(exc)}
            logger.error("bridge TLS materialization failed: %s", exc)
            return
        if not files:
            return
        self._bridge_extra_env.update(files)
        self.bridge._env.update(files)
        self._tls_material = {"ok": True, "error": ""}

    def start(self) -> None:
        self._materialize_bridge_tls()
        self.bridge.start()
        if self.sidecar_enabled:
            self.sidecar.start()
        else:
            logger.warning("sidecar disabled by BRIDGE_SIDECAR_ENABLED")
        if self.sim_enabled:
            threading.Thread(target=self._start_sim_phone, daemon=True).start()

    def _start_sim_phone(self) -> None:
        """拉起手机模拟器：先生成中文提示音，再以 phone 角色进同一个 TRTC 房间。

        它会先 `POST /api/v1/voice/session` 建立会话并把待领意图排给控制面，
        云端 sidecar 轮询领取后进入同一房间 —— 两端由此会合，全程无需真机。
        """
        # 先备好设备身份：真实手机是先配对再进房，模拟器走同一条路（服务端要求一致）。
        # 失败只记录「阶段:码」，绝不把凭证写进日志或状态。
        try:
            device = sim_provision.resolve_sim_device(
                base_url=self.sign_url,
                explicit_token=self.sim_device_credential,
                owner_credential=self.sim_owner_credential,
                device_name=self.sim_device_name,
            )
        except Exception as exc:  # str(exc) 只会是 "stage:code"
            logger.error("sim provisioning failed: %s", exc)
            with self._sim_lock:
                self.sim_metrics.state = "failed"
                self.sim_metrics.failure = f"provision:{exc}"
            return

        # 非敏感信息：回报 device_id，用于确认「配对确实成功」这一步。
        self._sim_device_id = device.device_id

        try:
            prompt = sim_phone.ensure_prompt_wav(
                self.sim_prompt_wav,
                text=self.sim_prompt_text or sim_phone.DEFAULT_PROMPT_TEXT,
            )
        except Exception as exc:
            logger.error("sim prompt generation failed: %s", exc)
            with self._sim_lock:
                self.sim_metrics.state = "failed"
                self.sim_metrics.failure = f"prompt_generation:{type(exc).__name__}"
            return

        self.sim_phone = Child(
            "sim-phone",
            [
                "xvfb-run", "-a",
                str(SIDECAR_DIR / "node_modules" / ".bin" / "electron"),
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                # 让 Chromium 把渲染进程 console 直接写到 stderr：无头环境里渲染进程
                # 日志既不进 stdout 也不一定落文件（实测注入目录一个文件都没生成，
                # stdout 只有 dbus 噪声），死因必须有个出口。仅模拟器需要，故只加在这里。
                "--enable-logging=stderr",
                ".",
                "--role=phone",
                # --device 必须是**注册返回的 device_id（UUID）**：服务端按它索引设备，
                # 用 "jax-sim-phone" 这种名字会被判未知设备而在 /session 被拒。
                f"--device={device.device_id}",
                # 必须显式传控制面地址：config.js 的默认值是本地 https://127.0.0.1:8000，
                # 漏传会让模拟器去连容器本机的 8000（那里什么都没有），/session 永远发不出去。
                f"--sign-url={self.sign_url}",
                f"--wav={prompt}",
                f"--out-wav={self.sim_out_wav}",
                f"--hold={self.sim_hold_s}",
                f"--join-grace={self.sim_join_grace_s}",
            ],
            SIDECAR_DIR,
            # 凭证只走环境变量：argv 会进日志（Child.start 会打印整条命令），env 不会。
            {
                "VOICE_SIM_DEVICE_CREDENTIAL": device.credential_token,
                "JAX_SIDECAR_LOG_DIR": str(getattr(self, "sim_log_dir", "/tmp/sim-logs")),
            },
            liveness=False,  # 跑完即退出是预期
        )
        try:
            self.sim_phone.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败必须能被 status 读到
            # 这里曾经没有兜住：Popen 抛错会静默杀死后台线程，status 永远停在
            # "pending"，与「正在跑」无法区分——正是本项目反复吃过的静默失败模式。
            logger.error("sim phone launch failed: %s", type(exc).__name__)
            with self._sim_lock:
                self.sim_metrics.state = "failed"
                self.sim_metrics.failure = f"launch:{type(exc).__name__}"

    def _reap_sim_phone(self) -> None:
        """模拟器结束（或仍在跑）时刷新指标；失败只记录，不影响容器存活。

        指标同时取自**子进程 stdout** 与**日志文件**（`sidecar-phone.log`）：
        无头容器里渲染进程的 stdout 不可靠（`sidecar/logger.js` 的既有结论），
        只抓 stdout 会永远解析不到任何指标。
        """
        child = self.sim_phone
        if child is None:
            return
        child.reap()  # 记录退出码（若已退出）
        with self._sim_lock:
            metrics = sim_phone.parse_phone_log(self._sim_lines(child))
            if child.exit_code is not None and metrics.state in ("pending", "joined"):
                metrics.state = "no_reply"
                metrics.notes.append(f"exited={child.exit_code}")
            if metrics.state != "pending":
                self.sim_metrics = metrics

    def _sim_lines(self, child: Child) -> list[str]:
        """stdout 尾部 ∪ 模拟器日志目录下的全部 `*.log`。

        为什么读整个目录、而不是只读 `sidecar-phone.log`：渲染进程若在**加载阶段**就
        失败，`sidecar-phone.log` 根本不会被创建，真正的死因（`[console:...]`、
        `net::ERR_*`）写在 `main.js` 的 `sidecar-main-diag.log` 里。只认一个文件名会
        把「渲染器没起来」和「起来了但业务失败」混为一谈——实测两者都表现为 `pending`。
        """
        lines = list(child.tail)
        for log_dir in self._sim_log_dirs():
            for name in _SIM_LOG_FILES:
                log_file = log_dir / name
                try:
                    if log_file.is_file():
                        lines.append(f"===== {name} =====")
                        lines.extend(
                            log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        )
                except OSError:
                    continue
        return lines

    def _sim_log_dirs(self) -> list[Path]:
        """**只认注入目录**，不做任何兜底。

        曾经兜底 `sidecar/logs`，实测被历史 `sidecar-phone.log` 污染，解析出与本次运行
        无关的「replied / reply 103 帧」。宁可看到 `log_files: []`（明确表示注入变量
        没传到位，是个可修的问题），也不要读来路不明的日志冒充证据。
        """
        return [Path(getattr(self, "sim_log_dir", "/tmp/sim-logs"))]

    def _sim_log_files(self) -> list[str]:
        """回报实际存在的手机日志文件——用来确认日志究竟落在哪个目录。"""
        found: list[str] = []
        for log_dir in self._sim_log_dirs():
            for name in _SIM_LOG_FILES:
                try:
                    if (log_dir / name).is_file():
                        found.append(f"{log_dir}/{name}")
                except OSError:
                    continue
        return found

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
            if self.sim_enabled:
                self._reap_sim_phone()
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
        """只把 liveness 子进程的死当失败；模拟器等一次性子进程退出不算。"""
        if self.bridge.liveness and self.bridge.reap() is not None:
            return self.bridge
        if self.sidecar_enabled and self.sidecar.liveness and self.sidecar.reap() is not None:
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
            # TLS 材料落盘结果（不含敏感值）：部署门禁据此读死因。
            "tls_material": getattr(self, "_tls_material", {"ok": True, "error": ""}),
        }
        payload["ok"] = bool(
            self.bridge.alive()
            and bridge_health == "ok"
            and (self.sidecar.alive() if self.sidecar_enabled else True)
        )
        if self.sim_enabled:
            with self._sim_lock:
                simulation = self.sim_metrics.to_dict()
            # 子进程输出必须可见：sim-phone 是唯一 liveness=False 的子进程，它若静默
            # 死掉，只暴露指标会看到 "pending" 而无从知道为什么。
            sim_child = getattr(self, "sim_phone", None)
            simulation["child"] = sim_child.describe() if sim_child is not None else None
            if sim_child is not None:
                # 渲染进程日志尾部：无头环境 stdout 拿不到，死因只能从这里读。
                simulation["log_tail"] = self._sim_lines(sim_child)[-30:]
                simulation["log_files"] = self._sim_log_files()
            simulation["device_id"] = getattr(self, "_sim_device_id", "")
            payload["simulation"] = simulation
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
