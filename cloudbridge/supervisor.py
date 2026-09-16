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
import audio_env  # noqa: E402
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

# ---------------------------------------------------------------------------
# 事件环（按标记过滤）——为什么必须有第二个环
# ---------------------------------------------------------------------------
# `output_tail` 是「最近 N 行」的**无差别**环，它的容量对齐的是「崩溃现场的全部上下文」。
# 但真实 sidecar 打开 --enable-logging=stderr 后，TRTC 音量回调**每 500ms** 打一条
# `[VOL] [:0] total=0`（实测：sidecar/logs/sidecar-sidecar.log），300 行只够盖约
# 2 分钟 ⇒ `[ROOM] 进房成功（elapsed=…）/ 进房失败 errCode=…`、`[SIG] 意图轮询`、
# `[PEER] 远端加入`、`[BOOT] role=sidecar` 这些「一行定生死」的行全被挤出窗口。
# 而本 CloudRun 的 CLS 主题只支持 queryString="*" 全量检索，关键词过滤返回 null
# （已实测）——外部**没有任何手段**能把那一刻的行捞回来。
# ⇒ 判据必须由 `/status` 自己带出来：这里再维护一个**只装可判定事件**的环，
#    条数按「事件」计，因此它覆盖的时间尺度比 output_tail 大一个量级。
EVENT_TAIL_LINES = 200

# 事件标记集合（大小写不敏感的**子串**匹配）。
#
# **为什么明确排除 `[VOL]`**：TRTC 音量回调每 500ms 一条，只贡献体积、不贡献信息，
# 正是它把 300 行的 output_tail 压到约 2 分钟；放进事件环等于把进房/信令事件再挤出去
# 一次。音量曲线若真需要，应另开一条带采样的专用通道，而不是污染事件账本。
# （判定顺序也由此固定：先挡噪声，再看标记——见 is_event_line。）
EVENT_MARKERS = (
    "BOOT", "SIG", "ROOM", "PEER", "PCM", "UPRMS", "STAT",
    "ERR", "WARN", "error", "Error", "FATAL",
    "失败", "进房", "errCode", "hello-redeem", "rtc session", "ws connected",
    "Cannot find module",
    # ADEV：本端音频设备表（sidecar/adev.js）。2026-09-16 事故里「手机在发、对端收不到」
    # 的真因是**本端没有可用播放设备**，而那一行此前只存在于 TRTC 的原生日志中。
    # 单独给一个标记，是为了让「进房后设备清点」这一行在 300 行 output_tail 被
    # `[VOL]`（每 500ms 一条）挤空之后仍然可读 —— 判据必须能被一条 GET 取到。
    "ADEV",
)

# 不进事件环的高频噪声行。`[VOL]` 独立成条：即使某条音量行里偶然带了别的标记词，
# 也不得因此漏进事件环（「每 500ms 一条」的噪声一旦漏进来就重演今天的问题）。
_EVENT_NOISE_RE = re.compile(r"\[VOL\]", re.IGNORECASE)

_LOWERED_EVENT_MARKERS = tuple(marker.lower() for marker in EVENT_MARKERS)

# 进房结果行的判据与取值（`/status.sidecar.last_join` 的唯一来源）。
# 真实格式（sidecar/rtc.js:207 / :221）：
#   `[ROOM] 进房成功（elapsed=348ms）` / `[ROOM] 进房失败 errCode=-1001`
_JOIN_SUCCESS_MARK = "进房成功"
_JOIN_FAILURE_MARK = "进房失败"
_JOIN_ELAPSED_RE = re.compile(r"(\d+)\s*ms", re.IGNORECASE)
_JOIN_ERRCODE_RE = re.compile(r"errCode\s*[:=]\s*(-?\d+)", re.IGNORECASE)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def is_event_line(line: str) -> bool:
    """这一行是否值得进事件环：先挡高频噪声，再看是否命中任一标记。

    噪声判定**优先于**标记匹配：音量行里若偶然出现标记词，也不得漏进来。
    """
    if _EVENT_NOISE_RE.search(line):
        return False
    lowered = line.lower()
    return any(marker in lowered for marker in _LOWERED_EVENT_MARKERS)


def extract_last_join(events: list[str]) -> dict | None:
    """从事件环里取**最后一次**进房结果，让一条 GET /status 直接回答「进房了没有」。

    为什么单拎成一个字段：读的人不该去 200 行里翻行序，也不该依赖窗口还剩多少——
    2026-09-16 的误判正是这么发生的（掐秒抓日志、还抓错了一次）。

    - 最后一次为准：失败那次必须盖掉成功那次，否则「上一轮成功、这一轮失败」会被
      读成一切正常。
    - 解析不出 elapsed/errCode 时对应字段为 `None`，**原文始终原样保留**（至少还能看）。
    - 从未出现过进房行 ⇒ 返回 `None`；这与「尝试过但失败」是两件不同的事。
    """
    for line in reversed(events):
        if _JOIN_SUCCESS_MARK not in line and _JOIN_FAILURE_MARK not in line:
            continue
        elapsed = _JOIN_ELAPSED_RE.search(line)
        errcode = _JOIN_ERRCODE_RE.search(line)
        return {
            "outcome": "failure" if _JOIN_FAILURE_MARK in line else "success",
            "line": line,
            "elapsed_ms": int(elapsed.group(1)) if elapsed else None,
            "err_code": int(errcode.group(1)) if errcode else None,
        }
    return None


class Child:
    """被监督的子进程：记录启动参数、退出码、重启次数，并保留输出尾部。

    为什么要保留输出尾部：容器崩溃后由平台重启，**上一次的 stdout 会随容器消失**
    且该服务的输出不进入可检索日志（实测 CLS 只有网关访问日志）。若不在进程内留住
    证据，就只能靠猜。状态端点会把这些尾部回报出来——崩溃自我解释，不依赖平台日志。

    这里维护**两个**环（并列，互不改写）：
      - `output_tail`：最近 N 行，无差别，回答「最后发生了什么」；
      - `events`：只装命中标记的可判定事件（见 EVENT_MARKERS），回答「关键事件发生过
        什么」。它存在的理由与 `[VOL]` 噪声有关，详见本文件顶部的说明。
    """

    TAIL_LINES = 120
    # 事件环容量（为什么是「按事件计」而不是「按行计」，见 EVENT_TAIL_LINES 说明）。
    # 设为类属性，便于替身与测试覆盖而不必改构造签名。
    EVENT_LINES = EVENT_TAIL_LINES

    def __init__(self, name: str, argv: list[str], cwd: Path, extra_env: dict[str, str],
                 *, liveness: bool = True, tail_lines: int | None = None) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        # 尾部窗口长度按子进程分别设定：真实 sidecar 打开 --enable-logging=stderr 后，
        # 渲染进程的每一条 console 都会进这里（这正是我们要的），固定的 120 行会被
        # 高频的 [STAT]/[UPRMS] 挤满，把"进房失败 errCode="那种一行定生死的死因挤出窗口。
        self.tail_lines = tail_lines or self.TAIL_LINES
        # liveness=False 的子进程是"一次性任务"（如手机模拟器）：它跑完就退出是**预期行为**，
        # 不得据此判定整体失败并触发容器重启。
        self.liveness = liveness
        self.exit_code: int | None = None
        self.starts = 0
        self.tail: list[str] = []
        self.events: list[str] = []
        self._lock = threading.Lock()
        env = dict(os.environ)
        env.update(extra_env)
        self._env = env
        self.proc: subprocess.Popen | None = None

    def _ingest(self, line: str) -> None:
        """把一行输出写进两个环：无差别的 `output_tail` 与按标记过滤的 `events`。

        两个环都要：`output_tail` 是「崩溃现场的全部上下文」，**语义与容量保持不变**
        （已有人在读它）；`events` 是事件账本，无论过多久都能被一条 GET 读到。
        """
        is_event = is_event_line(line)
        with self._lock:
            self.tail.append(line)
            if len(self.tail) > self.tail_lines:
                del self.tail[: len(self.tail) - self.tail_lines]
            if not is_event:
                return
            self.events.append(line)
            if len(self.events) > self.EVENT_LINES:
                del self.events[: len(self.events) - self.EVENT_LINES]

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
                self._ingest(line)
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
            # 替身（`Child.__new__`，既有契约测试就是这么构造的）没有 events；
            # 与 audio / _tls_material 的容错读法保持一致。
            events = list(getattr(self, "events", []))
        return {
            "alive": self.alive(),
            "pid": None if self.proc is None else self.proc.pid,
            "starts": self.starts,
            "exit_code": self.exit_code,
            "output_tail": tail,
            # events：只装可判定事件（进房/信令/对端/音频/错误…），滤掉 `[VOL]` 这类
            # 高频噪声。它与 output_tail 并列：后者是「最近 N 行」，会被噪声按时间挤空；
            # 前者按事件条数存活，因此窗口覆盖的时间尺度大一个量级。
            "events": events,
            # last_join：最近一次进房结果（成功/失败 + elapsed + errCode），
            # 一次 GET /status 即可判定，不必再去翻尾巴。从未进房则为 null。
            "last_join": extract_last_join(events),
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

        # 音频子系统（PulseAudio）：TRTC 的 Linux 原生层没有它就不能初始化音频设备，
        # EnterRoom 永远完不成 ⇒ 手机与云端媒体面从不共处一室（2026-09-16 事故，
        # 详见 cloudbridge/audio_env.py）。它必须在 sidecar **之前**就绪。
        self.audio: Child | None = None
        self._audio_plan: audio_env.AudioPlan | None = None
        self._audio_status: dict = {"ok": False, "state": "not-started", "error": ""}
        self.audio_runtime_dir = audio_env.default_runtime_dir()
        try:
            self.audio_ready_timeout_s = float(_env("JAX_AUDIO_READY_TIMEOUT_S", "20") or 20)
        except ValueError:
            self.audio_ready_timeout_s = audio_env.READY_TIMEOUT_S

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
                # 渲染进程的 console 必须进 stdout。此前只有内置模拟机带这个开关，
                # 真实 sidecar 的渲染进程 JS 日志既不进 stdout、也不在 /status 里
                # ——「进房成功/进房失败 errCode=」「[PCM]」「[UPRMS]」「[STAT]」全都看不见，
                # 这是 2026-09-16 排查里最大的观测障碍。与模拟机保持完全一致。
                "--enable-logging=stderr",
                ".",
                "--role=sidecar",
                f"--bridge-url={self.bridge_ws}",
                f"--sign-url={self.sign_url}",
            ],
            SIDECAR_DIR,
            {},
            # 打开渲染进程日志后行数陡增，尾部窗口放大一倍多，保证"进房失败 errCode="
            # 这类一行定生死的死因不会被高频状态行挤出 /status.sidecar.output_tail。
            tail_lines=300,
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

    def _start_audio(self) -> None:
        """在 sidecar 之前把音频子系统拉起来，并等它**真的可连**。

        为什么必须先起：TRTC 的 Linux 原生层在 EnterRoom 里初始化音频设备管理器，
        连不上 PulseAudio 就 `GetDevices wait`，进房**永远完不成**（26 分钟零原生日志），
        手机侧 user size 恒为 1，桥侧 `up rms=` 一条都不出现——手机和云端媒体面
        从来没共处一室（2026-09-16 事故）。

        fail-closed：起不来就在 stdout 打 FATAL 并让容器退出（进 CLS，一条日志可判定），
        绝不带着一个哑的音频层继续——那正是本次事故藏了 26 分钟的形态。
        """
        if not self.sidecar_enabled:
            # 没有 TRTC 对端就不需要音频层；显式记一条，避免以后误读成"音频起过"。
            logger.info("[audio] skipped: BRIDGE_SIDECAR_ENABLED=false（无 TRTC 对端，无需音频子系统）")
            self._audio_status = {"ok": True, "state": "skipped", "error": ""}
            return

        try:
            plan = audio_env.build_plan(self.audio_runtime_dir)
        except audio_env.AudioSubsystemError as exc:
            self._audio_status = {"ok": False, "state": "unavailable", "error": str(exc)}
            logger.error("[audio] FATAL: %s", exc)
            raise SystemExit(1) from exc

        self._audio_plan = plan
        # 与其它子进程同等的监督语义：pulseaudio 死了容器即退出（绝不带着哑音频层跑）。
        self.audio = Child("pulseaudio", plan.argv, SERVER_ROOT, plan.env)
        try:
            self.audio.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败必须 fail-closed
            self._audio_status = {"ok": False, "state": "launch_failed",
                                  "error": f"{type(exc).__name__}"}
            logger.error("[audio] FATAL: pulseaudio 无法启动（%s）；拒绝在无音频子系统的容器里"
                         "启动 sidecar —— TRTC 的 EnterRoom 会永远完不成", type(exc).__name__)
            raise SystemExit(1) from exc

        if not audio_env.wait_for_socket(plan.socket_path, timeout=self.audio_ready_timeout_s,
                                        alive=self.audio.alive):
            self._audio_status = {"ok": False, "state": "not_ready",
                                  "socket": str(plan.socket_path), "error": "socket_absent"}
            logger.error("[audio] FATAL: pulseaudio 在 %ss 内未就绪（socket=%s 未出现）；"
                         "拒绝启动 sidecar —— TRTC 只会在 `GetDevices wait` 上卡死",
                         self.audio_ready_timeout_s, plan.socket_path)
            raise SystemExit(1)

        # 路径必须先落进本进程 env：sidecar 由 xvfb-run 派生，多一跳继承最容易丢变量。
        # （Child 在构造时已拷贝 os.environ，所以下面还要显式更新它的 _env。）
        os.environ.update(plan.env)
        self.sidecar._env.update(plan.env)
        self._audio_status = {
            "ok": True,
            "state": "ready",
            "sink": plan.sink,
            "source": plan.source,
            "socket": str(plan.socket_path),
            "server": plan.server,
            "runtime_dir": str(plan.runtime_dir),
            "error": "",
            # 实测清点结果（见 _probe_audio_devices）。**必须**与上面的 sink/source 分开看：
            # 上面两个字段来自启动计划（"我们打算造什么"），devices 才是"真的造出来了什么"。
            "devices": self._probe_audio_devices(plan),
        }
        logger.info("[audio] pulseaudio ready, sink=%s, socket=%s, server=%s",
                    plan.sink, plan.socket_path, plan.server)

    def _probe_audio_devices(self, plan: audio_env.AudioPlan) -> dict:
        """运行期清点音频设备，把「PA 里到底有没有 sink/source」变成一条 GET 可读的事实。

        为什么必须有这一步（2026-09-16 事故的最大观测缺口）
        -------------------------------------------------
        此前 `/status.audio` 里的 `sink` / `source` **来自启动计划，不是实测**：它只能证明
        "计划里写了这个 sink"，证明不了"PA 里真的有"。而 TRTC 的播放设备枚举正是从 PA 拿的
        —— 构建期自证通过 ≠ 运行期有设备。事故现场这两件事同时成立：
        `/status.audio` 报告 `ok=true, sink=jax_null`，而 sidecar 侧是
        `player device list is empty`（code 1202）+ `up=0帧` 连续 70 秒。
        口径混淆到这一步，一条 GET 就判不了死，只能去猜。
        所以这里用构建期自证**同一个** `inspect_devices()` 再实测一次，并如实报出去。

        失败语义：清点失败只写进 `devices.error` / `probed=false`，**绝不**影响启动与退出
        （观测不得成为新的故障面）。但"清点成功且一个 sink 都没有"会被打成正 ERROR 一行：
        那正是"远端音频帧永不回调、上行恒为 0"的直接条件。
        """
        try:
            report = audio_env.inspect_devices(env=plan.env, sink=plan.sink)
        except Exception as exc:  # noqa: BLE001 - 观测分支一律 fail-open
            logger.warning("[audio] 设备清点异常（不影响启动）：%s", type(exc).__name__)
            return audio_env.summarize_devices(None, error=f"{type(exc).__name__}: {exc}")
        summary = audio_env.summarize_devices(report)
        if summary["playout_ok"] is False:
            logger.error(
                "[audio] 清点结果 playout_ok=False：PA 里没有任何 sink=%s 前缀的设备 ⇒ "
                "TRTC 的播放设备枚举会拿到空表 ⇒ 远端音频帧永不回调（onPlayAudioFrame）、"
                "上行恒为 0。devices=%s", plan.sink, summary,
            )
        else:
            logger.info("[audio] 设备清点 devices=%s", summary)
        return summary

    def start(self) -> None:
        self._materialize_bridge_tls()
        # 音频子系统必须先于 sidecar 就绪（TRTC 进房即用音频设备）。
        self._start_audio()
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

        # 凭证只走环境变量：argv 会进日志（Child.start 会打印整条命令），env 不会。
        sim_env = {
            "VOICE_SIM_DEVICE_CREDENTIAL": device.credential_token,
            "JAX_SIDECAR_LOG_DIR": str(getattr(self, "sim_log_dir", "/tmp/sim-logs")),
        }
        # 模拟器与 sidecar 同容器、同走 TRTC 原生层，音频子系统变量必须一并注入。
        if getattr(self, "_audio_plan", None) is not None:
            sim_env.update(self._audio_plan.env)
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
                # stdout 只有 dbus 噪声），死因必须有个出口。真实 sidecar 也已同款打开。
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
            sim_env,
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

    def _children(self) -> list[Child]:
        """接受监督的全部子进程（含音频子系统）。容器内没有自愈，退出即整体退出。"""
        children = [self.sidecar, self.bridge]
        audio = getattr(self, "audio", None)
        if audio is not None:
            children.append(audio)
        return children

    def terminate_all(self) -> None:
        self.shutting_down = True
        for child in self._children():
            child.signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        for child in self._children():
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
        # 音频子系统死掉同样是致命的：TRTC 的原生层随后会退化成 `GetDevices wait`，
        # 进房永远完不成——这种"进程都活着但媒体面死了"的状态最难查，所以直接整体退出。
        audio = getattr(self, "audio", None)
        if audio is not None and audio.liveness and audio.reap() is not None:
            return audio
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
            # 音频子系统状态：判定"手机与云端是否真的能在同一房间"的第一块拼图。
            # 没有它，TRTC 的 EnterRoom 会卡在 `GetDevices wait` 上永不完成。
            "audio": getattr(self, "_audio_status",
                             {"ok": False, "state": "not-started", "error": ""}),
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
