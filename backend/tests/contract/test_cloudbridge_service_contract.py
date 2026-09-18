"""jax-voice-bridge（云端无头 TRTC 对端）的结构与行为契约。

背景：sidecar + rtc_bridge 原本跑在用户 Windows 机器上，靠 `jax-watchdog.ps1` 与
计划任务维持存活，另有一堆 .ps1/.bat 本地脚本兜底。用户已要求消灭一切本地运行态，
把这两个进程原样搬进同一容器。本文件把搬迁后的关键性质变成可执行断言：

1. 构建输入只有受跟踪源码（白名单精确到 sidecar 的 *.js/*.html/*.json）；
2. 本机 node_modules（Windows 二进制）绝不进镜像；
3. 子进程退出即整体退出——用平台重启替代本地 watchdog，不写自愈脚本；
4. 状态由服务自身报告（/api/v1/voice/bridge/status），供部署门禁判定。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"

FORBIDDEN_LOCAL_TOKENS = (
    "adb reverse",
    "adb connect",
    "tailscale",
    "jax-watchdog",
    "jax-services.ps1",
    "install-scheduled-tasks",
    "schtasks",
)


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "jax_voice_bridge_supervisor", CLOUDBRIDGE / "supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 1. 构建输入只有受跟踪源码 ---------------------------------------------


def test_manifest_declares_tracked_source_and_forbids_local_node_modules() -> None:
    manifest = json.loads((CLOUDBRIDGE / "CANONICAL_SOURCE.json").read_text(encoding="utf-8"))
    assert manifest["service"] == "jax-voice-bridge"
    assert manifest["dockerfile"] == "cloudbridge/Dockerfile"
    assert manifest["entrypoint"] == "cloudbridge/supervisor.py"
    assert "sidecar/node_modules" in manifest["forbidden_paths"]
    for relative_path in manifest["required_paths"]:
        assert (ROOT / relative_path).exists(), relative_path


def test_dockerfile_copies_every_tree_the_service_needs() -> None:
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    for copied in ("backend/app", "backend/rtc_bridge", "sidecar", "config", "cloudbridge"):
        assert f"COPY {copied} " in text, f"镜像缺少 COPY {copied}"
    assert "xvfb" in text, "Electron 无头运行需要 xvfb 提供虚拟显示"
    assert "npm install" in text, "sidecar 依赖必须在镜像内安装（Linux 原生 addon）"


def test_dockerignore_allows_bridge_sources_and_excludes_windows_node_modules() -> None:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for allowed in (
        "!backend/rtc_bridge/**",
        "!cloudbridge/**",
        "!sidecar/*.js",
        "!sidecar/*.json",
    ):
        assert allowed in text, f"构建上下文缺少放行规则 {allowed}"
    assert "sidecar/node_modules" in text, "必须显式排除本机 node_modules"


def test_entrypoint_exposes_one_platform_port() -> None:
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 9200" in text
    assert 'CMD ["python", "cloudbridge/supervisor.py"]' in text


def test_dockerfile_installs_xauth_needed_by_xvfb_run() -> None:
    """实测：缺 xauth 时 `xvfb-run` 报 "xauth command not found"，sidecar 起不来。"""
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "xauth" in text


def test_sidecar_launches_with_the_container_required_chromium_switches() -> None:
    """实测：缺 --no-sandbox 时 Electron 直接 FATAL/SIGTRAP（容器内以 root 运行）。

    --disable-gpu 与 --disable-dev-shm-usage 同理：容器无 GPU、/dev/shm 很小。
    这三项是无头容器里跑 Electron 的必需开关，不是可选项。
    """
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    for flag in ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"):
        assert flag in text, f"sidecar 启动参数缺少 {flag}"


def test_tls_trust_anchor_is_pinned_and_shipped_in_the_image() -> None:
    """sidecar 以 NODE_EXTRA_CA_CERTS 做密码学级 pinning，缺失即 fail-closed 退出。

    锚必须是签发云端控制面 leaf 的那张中间 CA（不是系统 CA 包），并且要进镜像。
    """
    dockerfile = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "NODE_EXTRA_CA_CERTS=/srv/certs/cloud-control-plane-issuer.pem" in dockerfile
    assert "COPY certs/cloud-control-plane-issuer.pem" in dockerfile

    anchor = ROOT / "certs" / "cloud-control-plane-issuer.pem"
    assert anchor.is_file(), "缺少固定的 TLS 信任锚"
    pem = anchor.read_text(encoding="utf-8")
    assert pem.count("-----BEGIN CERTIFICATE-----") == 1, "锚文件必须是单张证书"

    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "!certs/cloud-control-plane-issuer.pem" in ignore, "锚必须在构建上下文白名单内"


def test_requirements_cover_the_rtc_bridge_closure_including_numpy() -> None:
    """实测：rtc_bridge/session.py → app.voice.apm_bridge 在模块级 import numpy，
    漏装会让容器启动即 ModuleNotFoundError 并整体退出。"""
    text = (CLOUDBRIDGE / "requirements.txt").read_text(encoding="utf-8")
    for requirement in ("websockets", "httpx", "numpy", "psycopg"):
        assert requirement in text, f"bridge 依赖缺少 {requirement}"


# --- 2. 不得出现本地绕行手段 ----------------------------------------------


def test_no_local_bypass_mechanisms_anywhere_in_the_service() -> None:
    for name in ("supervisor.py", "Dockerfile", "requirements.txt"):
        lowered = (CLOUDBRIDGE / name).read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_LOCAL_TOKENS:
            assert token not in lowered, f"{name} 出现本地/绕行手段: {token}"


def test_service_reads_credentials_only_from_environment() -> None:
    """容器内不得读取 .env 文件——凭据一律由 CloudRun 环境变量注入。

    注意别用裸子串 ".env" 判定：`os.environ` 本身就包含它。
    """
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    assert "dotenv" not in text
    for quoted in ('".env"', "'.env'", "/.env", "\\.env"):
        assert quoted not in text, f"supervisor 不应引用本地凭据文件: {quoted}"


# --- 3. 子进程退出即整体退出（替代 watchdog）------------------------------


class _StubChild:
    def __init__(self, *, alive: bool = True, exit_code: int | None = None,
                 name: str = "child") -> None:
        self.name = name
        self._alive = alive
        self._exit_code = exit_code
        self.exit_code = exit_code
        self.starts = 1
        self.pid = 4242
        self.kwargs = {"extra": "stub"}
        self.liveness = True

    def alive(self) -> bool:
        return self._alive

    def reap(self):
        return self._exit_code

    def describe(self) -> dict:
        return {"alive": self._alive, "pid": self.pid, "starts": self.starts,
                "exit_code": self._exit_code}

    def signal(self, _sig) -> None:
        self._alive = False


def test_watch_exits_nonzero_when_rtc_bridge_dies() -> None:
    """rtc_bridge 死掉必须整体退出——由平台重启，绝不本地自愈。"""
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = True
    sup.crash_grace_s = 0
    sup.bridge = _StubChild(alive=False, exit_code=1)
    sup.sidecar = _StubChild(alive=True)

    with pytest.raises(SystemExit) as excinfo:
        sup.watch()
    assert excinfo.value.code == 1


def test_watch_exits_nonzero_when_sidecar_dies() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = True
    sup.crash_grace_s = 0
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=False, exit_code=137)

    with pytest.raises(SystemExit) as excinfo:
        sup.watch()
    assert excinfo.value.code == 1


# --- 4. 状态由服务自身报告 -------------------------------------------------


def test_status_reports_children_health_and_verdict() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = "https://example.invalid"
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")

    payload = sup.status()
    assert payload["service"] == "jax-voice-bridge"
    assert payload["ok"] is True
    assert set(payload) >= {
        "rtc_bridge", "sidecar", "rtc_bridge_health", "sign_url",
        "device_id", "trtc_sdk_version", "uptime_s", "sidecar_enabled",
    }


def test_status_is_not_ok_when_health_probe_fails() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = ""
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    object.__setattr__(sup, "_probe_bridge_health", lambda: "URLError")

    assert sup.status()["ok"] is False


def test_sdk_version_comes_from_the_sidecar_manifest() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    version = sup.sdk_version()
    assert version, "必须能从 sidecar/package.json 读到 TRTC SDK 版本"


# --- 5. 崩溃必须自我解释（平台不收集容器 stdout）--------------------------


def test_child_describe_exposes_output_tail() -> None:
    """死因必须留在产品自己能报出来的地方：容器 stdout 不在可检索日志里。"""
    module = _load_supervisor()
    child = module.Child.__new__(module.Child)
    child.name = "sidecar"
    child.starts = 1
    child.exit_code = 2
    child.proc = None
    child._lock = __import__("threading").Lock()
    child.tail = ["[main] fatal NODE_EXTRA_CA_CERTS is not set"]

    described = child.describe()
    assert described["output_tail"] == ["[main] fatal NODE_EXTRA_CA_CERTS is not set"]
    assert described["exit_code"] == 2


def test_watch_holds_the_status_endpoint_before_exiting() -> None:
    """子进程死亡后必须先留出可观测窗口，再退出交给平台重启。"""
    import time

    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = False
    sup.crash_grace_s = 0.3
    sup.bridge = _StubChild(alive=False, exit_code=1)
    sup.sidecar = _StubChild(alive=True)

    started = time.monotonic()
    with pytest.raises(SystemExit):
        sup.watch()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.25, "宽限期内不得立刻退出，否则死因来不及被读到"


def test_sidecar_is_not_given_the_phone_only_device_argument() -> None:
    """实测事故：role=sidecar 带 --device 会被 sidecar 自身判 SIDECAR_UNEXPECTED_DEVICE_ARG
    并 fail-closed 退出（config.js:64 / rtc.js:364），导致容器崩溃重启。

    `--device` 只属于 role=phone。产品运行期里**没有**任何 phone 角色子进程了
    （手机模拟器已于 2026-09-17 整体移除，见 test_bridge_sim_phone_removed_contract.py），
    所以这里只剩一条：sidecar 不得带 --device=；`--role=phone` 在 supervisor 里
    必须一个都不剩。
    """
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    sidecar_block = text.split("self.sidecar = Child(", 1)[1].split(")", 1)[0]
    assert "--device=" not in sidecar_block, "sidecar 启动参数里不得出现 --device="
    code = ast.parse(text)  # 看代码构造，不看注释（注释里解释历史是允许的）
    literals = {n.value for n in ast.walk(code)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "--role=phone" not in literals, (
        "容器内不得再以 phone 角色起 Electron —— 那会抢 sidecar 唯一的会话位")
    assert "sim-phone" not in literals, "容器内不得再有名为 sim-phone 的子进程"


def test_bridge_health_port_stays_loopback_only() -> None:
    """rtc_bridge 的 19092/19093 只在容器内可达，不对外暴露。"""
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    assert "ws://127.0.0.1:19092" in text
    assert "127.0.0.1:19093" in text


# --- 6. 手机模拟的**遗留部分**（模块保留给容器外的本地 harness）-------------
#
# 2026-09-17：容器内的手机模拟器已从产品运行期整体移除
# （supervisor 的启动路径 / `SIM_*` 环境变量 / `/status.simulation` 全部删掉），
# 这里剩下的只有 `sim_phone.py` 的**离线解析**契约 —— 模块仍由
# `scripts/sim/run-phone.py` 使用，是容器之外唯一的端到端验证手段。
# 移除本身的契约在 test_bridge_sim_phone_removed_contract.py。


def _load_sim_phone():
    spec = importlib.util.spec_from_file_location(
        "jax_voice_bridge_sim_phone", CLOUDBRIDGE / "sim_phone.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ⚠️ 这段是**逐行来自真实抓取**，不是手写合成。
#   出处：outputs/deploy-backup-20260911/sidecar-logs-phone/sidecar-phone.log
#         （2026-09-16T00:44 一轮真实 TRTC 端到端模拟），
#         数字与 outputs/deploy-backup-20260911/e2e-summary.json 的 metrics 段三方互证
#         （远端就绪 2331 / 进房 348 / 首包 4577 / 上行 156 / 回复 151 / 有效语音 64）。
#   格式是 sidecar/logger.js:19 的 `[ISO] [scope] msg`，即**真机上带 `[PHONE] ` 前缀**。
#   旧版本这里写的是凭空合成的「上行 350帧 / 首包 3120ms」——那不是任何一次真实抓取，
#   而且它把「phone.js 补的 2s 尾部静音」当成计入了上行帧（据此算出 speech_ms=5000），
#   前提是错的：phone.js:114 的 upFrames 只在 wav 分帧循环里加，:123-129 的静音循环不计帧。
REAL_PHONE_LOG = [
    "[2026-09-16T00:44:20.815Z] [PHONE] 进房 room=jax-f068f119 user=f068f119",
    "[2026-09-16T00:44:20.818Z] [PHONE] wav=/srv/sim/sim-prompt-run1.wav 99840B（16k s16）",
    "[2026-09-16T00:44:21.182Z] [PHONE] 进房成功 348ms",
    "[2026-09-16T00:44:23.105Z] [PHONE] 远端加入 jax-pc-sidecar",
    "[2026-09-16T00:44:23.152Z] [PHONE] 远端就绪 @2331ms",
    "[2026-09-16T00:44:26.439Z] [PHONE] wav 推完（156帧），补 2s 静音",
    "[2026-09-16T00:44:27.730Z] [PHONE] 首包回复 @4577ms（自上行开始）",
    "[2026-09-16T00:44:30.372Z] [PHONE] 回复结束 133帧/83KB（连续 1200ms 无新帧判定说完）",
    "[2026-09-16T00:44:30.745Z] [PHONE] 回复已保存: /srv/sim/sim-reply.wav（96640B）",
    "[2026-09-16T00:44:30.766Z] [PHONE] 有效语音 64帧/40960B（共收到 151帧/96640B，含静音帧）",
    "[2026-09-16T00:44:30.768Z] [PHONE] 上行 156帧 / 回复 151帧",
]


def test_phone_log_parsing_yields_comparable_latency() -> None:
    """首包延迟必须换算成「用户说完 → 听见」的口径，否则数字会被误读。

    口径（2026-09-16 用真实产物三方互证后修正）
    ------------------------------------------
    `phone.js` 报的 `首包回复 @Nms` 自**上行第一帧**起计；`up_frames` 只在 wav 分帧
    循环里 +1（phone.js:114），后补的 2s 尾部静音（phone.js:123-129）**一帧都不计**。
    证据是自洽的：wav=99840B ⇒ 99840 ÷ 2 ÷ 16000 = **3.12s**，同一份日志同时报
    `wav 推完（156帧）` 与 `上行 156帧`，156 × 20ms = **3120ms** 恰好等于 wav 时长。

    故：`utterance_ms == speech_ms == 3120`（旧实现把 3.12s 报成 1.12s，少 2000ms），
    `reply_after_speech_ms == 4577 - 3120 == 1457`（用户说完到听见首帧）。
    """
    metrics = _load_sim_phone().parse_phone_log(REAL_PHONE_LOG)
    assert metrics.state == "replied"
    assert metrics.first_reply_ms == 4577
    assert metrics.enter_room_ms == 348
    assert metrics.remote_ready_ms == 2331, (
        "真实落盘是 `[PHONE] ` 前缀形态；正则要求裸 `PHONE ` 时这里恒为 None"
    )
    assert metrics.up_frames == 156 and metrics.reply_frames == 151
    assert metrics.reply_bytes == 96640
    assert metrics.speech_reply_frames == 64 and metrics.speech_reply_bytes == 40960
    assert metrics.utterance_ms == 3120, "156 帧 × 20ms —— 就是被推上去的 wav 时长"
    assert metrics.speech_ms == 3120, (
        "尾部静音不计帧 ⇒ 不得再减 2000ms（旧实现实得 1120）"
    )
    assert metrics.reply_after_speech_ms == 4577 - 3120
    assert metrics.to_dict()["ok"] is True


def test_phone_log_parsing_detects_no_reply_and_failures() -> None:
    module = _load_sim_phone()
    assert module.parse_phone_log(["PHONE hold=45s 内未收到回复（或超时），退出"]).state == "no_reply"
    assert module.parse_phone_log(["PHONE PHONE_SESSION_SIGN_FAILED"]).failure == "PHONE_SESSION_SIGN_FAILED"
    assert module.parse_phone_log(["PHONE 进房失败 -1002"]).state == "failed"


def test_sim_phone_can_no_longer_affect_container_liveness() -> None:
    """**移除**：以前要论证"sim 是一次性（liveness=False）子进程、它退出不算死"。

    容器里现在根本没有这样的子进程了 —— `_first_dead()` 只看 rtc_bridge / sidecar /
    audio 三个 liveness 子进程，所以那个风险点连同 sim 子进程一起消失了。
    """
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = False
    sup.crash_grace_s = 0
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    sup.audio = None

    assert not hasattr(sup, "sim_phone"), (
        "容器里不得再有 sim 子进程句柄 —— 它存在就意味着那条执行路径回来了")
    assert not hasattr(sup, "sim_enabled"), "sim_enabled 这个开关本身也必须消失"
    assert sup._first_dead() is None, "liveness 子进程都健康时不得判死"


def test_no_phone_role_child_survives_in_the_supervisor() -> None:
    """**移除**：supervisor 不再有任何 phone 角色的启动参数，也不再构造 sim 子进程。

    删掉的是**执行路径**（以及 `--device` 这类 phone-only 参数），不是模块 ——
    模块仍由容器外的 `scripts/sim/run-phone.py` 使用。
    """
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    code = ast.parse(text)  # 只看代码构造：注释里解释历史是允许的
    literals = {n.value for n in ast.walk(code)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for gone in ("--role=phone", "--wav=", "--out-wav=", "--hold=", "--join-grace=",
                 "--device=", "sim-phone"):
        assert gone not in literals, f"supervisor 里仍残留 phone 角色的启动参数：{gone!r}"
    funcs = {n.name for n in ast.walk(code)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for gone in ("_start_sim_phone", "_reap_sim_phone", "_sim_lines"):
        assert gone not in funcs, f"supervisor 里仍有模拟器方法 {gone}()"
    assert "self.sim_phone = Child(" not in text


# 手机模拟器的提示音工具链（`edge-tts` + `ffmpeg`）——2026-09-18 从生产镜像回收
# ---------------------------------------------------------------------------
# 它们只服务一件事：**容器内**把中文提示音合成成 16k wav
# （落地点 cloudbridge/sim_phone.py:ensure_prompt_wav，调用方是 supervisor 已删除的
# `_start_sim_phone`）。模拟器本身已作为「产品运行期里的测试夹具」被整体移除，
# 于是容器里**再无任何运行期消费者**：
#   · cloudbridge 只跑 rtc_bridge + sidecar + audio；rtc_bridge 的 import 闭包
#     （session.py → app.voice.apm_bridge / qwen_realtime_bridge / end_detect）**不经过**
#     app.voice.half_duplex，而 `import edge_tts` 只发生在 app/voice/tts_edge.py:52
#     —— 那条链属于 FastAPI 语音网关，不在本镜像里；
#   · apt 的 `ffmpeg` 此前也只被 ensure_prompt_wav 用 subprocess 调起。镜像里出现的
#     `libtxffmpeg.so` 是 **TRTC 自带库**，与 apt 的 ffmpeg 毫无关系（别误删）。
# 本地 harness（scripts/sim/run-phone.py）在**容器外**运行，用宿主机的这两个工具，
# 不受本改动影响。
#
# 检查口径与 supervisor 那条移除检查器一致：**只看会被执行的内容**，注释里解释历史
# 是允许的 —— 否则"为什么删掉"这句话本身就会被自己的测试判红。

_PROMPT_AUDIO_TOKENS = ("ffmpeg", "edge-tts", "edge_tts")


def _dockerfile_executable_text(dockerfile_text: str) -> str:
    """剥掉 Dockerfile 的**整行注释**，只留真正会被执行的行。

    Dockerfile 里首个非空白字符是 `#` 的行不参与任何指令、也不参与续行拼接。
    剥掉后，`libtxffmpeg.so`（字符串本身含 "ffmpeg"）这类只出现在注释里的 TRTC 库名
    就不会误伤检查器。
    """
    return "\n".join(
        line for line in dockerfile_text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _prompt_audio_toolchain_violations(dockerfile_text: str) -> list[str]:
    """返回「镜像里仍带着提示音工具链」的违规点（空列表 = 干净）。"""
    body = _dockerfile_executable_text(dockerfile_text)
    violations: list[str] = []
    for token in _PROMPT_AUDIO_TOKENS:
        if token in body:
            where = [ln.strip() for ln in body.splitlines() if token in ln]
            violations.append(f"Dockerfile 的执行内容里仍有 {token!r}：{where}")
    return violations


def test_image_no_longer_ships_the_prompt_audio_toolchain() -> None:
    """**移除**：商用镜像不再带 ffmpeg / edge-tts —— 它们只服务已删掉的容器内模拟器。

    这一条正是原 `test_image_can_synthesise_the_prompt_audio_in_cloud` 的翻转：
    从「断言这两个工具**存在**」改为「断言它们**不存在**」。
    """
    dockerfile = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    violations = _prompt_audio_toolchain_violations(dockerfile)
    assert violations == [], (
        "生产镜像里还留着手机模拟器的提示音工具链（应随模拟器一起回收）：\n"
        + "\n".join(violations)
    )

    # 回收必须是**外科手术式**的：音频子系统与 TRTC 原生库的搜索路径一字未动。
    body = _dockerfile_executable_text(dockerfile)
    for kept in ("pulseaudio", "pulseaudio-utils", "pulseaudio-module-raop",
                 "xvfb", "xauth", "npm"):
        assert kept in body, f"回收 ffmpeg/edge-tts 时误伤了与本任务无关的依赖：{kept}"
    assert (
        "ENV LD_LIBRARY_PATH=/srv/sidecar/node_modules/trtc-electron-sdk/build/Release"
        in body
    ), "TRTC 原生库的 dlopen 搜索路径必须原样保留（libtxffmpeg.so 与 apt 的 ffmpeg 无关）"
    assert "-r cloudbridge-requirements.txt" in body, "运行期 Python 依赖闭包必须照旧安装"


def test_the_removal_checker_rejects_the_pre_removal_dockerfile() -> None:
    """检查器必须真的抓得住「删之前那一版」——否则它只是装饰。

    同时钉住三件事：
      ① apt 清单里加回 `ffmpeg`            → 判红；
      ② `pip install edge-tts==7.2.8` 加回 → 判红；
      ③ 而**注释里**提到这两个名字**不算违规** —— 否则"为什么删掉"就写不出口。
    """
    dockerfile = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")

    # ① apt 清单里加回 ffmpeg
    apt_anchor = "        npm \\\n"
    assert apt_anchor in dockerfile, "Dockerfile 的 apt 清单结构变了，请先更新本用例"
    with_apt = dockerfile.replace(apt_anchor, apt_anchor + "        ffmpeg \\\n", 1)
    violations = _prompt_audio_toolchain_violations(with_apt)
    assert violations, "apt 清单里加回 ffmpeg 竟然没被判红"
    assert "ffmpeg" in violations[0]

    # ② pip 装回 edge-tts
    pip_anchor = "RUN pip install --no-cache-dir -r cloudbridge-requirements.txt\n"
    assert pip_anchor in dockerfile, "Dockerfile 的 pip 安装段结构变了，请先更新本用例"
    with_pip = dockerfile.replace(
        pip_anchor,
        pip_anchor.rstrip("\n") + " \\\n"
        "    && pip install --no-cache-dir edge-tts==7.2.8\n",
        1,
    )
    violations = _prompt_audio_toolchain_violations(with_pip)
    assert violations, "pip 装回 edge-tts 竟然没被判红"
    assert any("edge-tts" in v for v in violations)

    # ③ 注释不算违规（顶部那段解释性注释也因此不许被误伤）
    assert _prompt_audio_toolchain_violations(
        dockerfile + "\n# 历史：这里曾装过 ffmpeg / edge-tts，现随模拟器一起回收\n"
    ) == [], "检查器把注释也当成了违规——那「为什么删掉」就写不出来了"


def test_status_no_longer_reports_simulation() -> None:
    """**移除**：`/status` 不再有 `simulation` 段 —— 无论环境变量怎么设。

    部署门禁读的是 ok / rtc_bridge / sidecar / rtc_bridge_health / trtc_sdk_version，
    移除 simulation 不得动它们；残留的模拟器环境变量则改由 `ignored_env`（只列名字）
    报出来，配置漂移仍然是"看得见、但不照做"。
    """
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = ""
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.ignored_env = ["BRIDGE_SIM_PHONE", "SIM_DEVICE_ID"]
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")

    payload = sup.status()
    assert "simulation" not in payload, "产品运行期不再有模拟器，也就没有 simulation 可报"
    assert not hasattr(sup, "sim_metrics"), "sim_metrics 这个状态对象也必须消失"
    for key in ("ok", "rtc_bridge", "sidecar", "rtc_bridge_health", "trtc_sdk_version"):
        assert key in payload, f"/status 少了部署门禁依赖的键：{key}"
    assert payload["ok"] is True, "两个 liveness 子进程都活着时 ok 必须为 True"
    assert payload["ignored_env"] == ["BRIDGE_SIM_PHONE", "SIM_DEVICE_ID"]


# --- 7. TLS 材料：PEM 走环境变量、启动时落受限临时文件（私钥绝不烘进镜像）------
#
# 背景：rtc_bridge 必须用 mTLS 调云端控制面完成 hello 兑付（fail-closed 终局），
# 但 client.key 是私钥，烘进镜像会把私钥固化进可被任意拉取者读到的镜像层。
# 因此 PEM 经环境变量注入，由 supervisor 启动时落成 0o600 临时文件并注入 *_FILE。

# 明显假值：仅用于断言「异常文本不泄漏 PEM」。带结尾换行以锁定 PEM 原样落盘。
FAKE_CERT_PEM = "-----BEGIN FAKE CERT-----\nMIIBfakecert\n"
FAKE_KEY_PEM = "-----BEGIN FAKE KEY-----\nMIIBfakekey\n"
FAKE_CA_PEM = "-----BEGIN FAKE CA-----\nMIIBfakeca\n"


def _load_tls_material():
    spec = importlib.util.spec_from_file_location(
        "jax_voice_bridge_tls_material", CLOUDBRIDGE / "tls_material.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_materialize_writes_three_files_with_strict_modes(tmp_path) -> None:
    module = _load_tls_material()
    env = {
        "RTC_BRIDGE_CLIENT_CERT_PEM": FAKE_CERT_PEM,
        "RTC_BRIDGE_CLIENT_KEY_PEM": FAKE_KEY_PEM,
        "RTC_BRIDGE_CONTROL_PLANE_CA_PEM": FAKE_CA_PEM,
    }
    result = module.materialize_tls_files(tmp_path, env)

    assert set(result) == {
        "RTC_BRIDGE_CLIENT_CERT_FILE",
        "RTC_BRIDGE_CLIENT_KEY_FILE",
        "RTC_BRIDGE_CONTROL_PLANE_CA_FILE",
    }
    for path in result.values():
        assert Path(path).is_absolute()
        assert Path(path).is_file()
    assert (tmp_path / "client.crt").read_text(encoding="utf-8") == FAKE_CERT_PEM
    assert (tmp_path / "client.key").read_text(encoding="utf-8") == FAKE_KEY_PEM
    assert (tmp_path / "ca.crt").read_text(encoding="utf-8") == FAKE_CA_PEM

    if os.name == "posix":
        # Windows 无 POSIX 权限位，os.chmod 后 st_mode 仍是 0o666/0o444，不可断言。
        assert (tmp_path / "client.key").stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_materialize_returns_empty_without_touching_disk_when_unconfigured(tmp_path) -> None:
    module = _load_tls_material()
    target = tmp_path / "never-created"
    assert module.materialize_tls_files(target, {}) == {}
    assert not target.exists(), "未配置时不得创建任何文件或目录"


def test_materialize_rejects_partial_config_without_leaking_pem(tmp_path) -> None:
    module = _load_tls_material()
    env = {
        "RTC_BRIDGE_CLIENT_CERT_PEM": FAKE_CERT_PEM,
        "RTC_BRIDGE_CONTROL_PLANE_CA_PEM": FAKE_CA_PEM,
        # 故意缺 RTC_BRIDGE_CLIENT_KEY_PEM
    }
    with pytest.raises(module.TlsMaterialError) as excinfo:
        module.materialize_tls_files(tmp_path, env)

    text = str(excinfo.value)
    assert "RTC_BRIDGE_CLIENT_KEY_PEM" in text, "异常消息必须点出缺失的键名"
    assert FAKE_KEY_PEM not in text
    assert FAKE_CERT_PEM not in text
    assert FAKE_CA_PEM not in text


def test_materialize_raises_when_target_is_not_writable(tmp_path) -> None:
    """目录不可写必须 fail-closed（用普通文件冒充父目录，跨平台稳定触发 OSError）。"""
    module = _load_tls_material()
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    env = {
        "RTC_BRIDGE_CLIENT_CERT_PEM": FAKE_CERT_PEM,
        "RTC_BRIDGE_CLIENT_KEY_PEM": FAKE_KEY_PEM,
        "RTC_BRIDGE_CONTROL_PLANE_CA_PEM": FAKE_CA_PEM,
    }
    with pytest.raises(module.TlsMaterialError):
        module.materialize_tls_files(blocker / "sub", env)


def test_supervisor_aliases_control_plane_base_url_for_bridge(monkeypatch) -> None:
    """服务上的是 CONTROL_PLANE_BASE_URL，rtc_bridge 读的是 RTC_BRIDGE_CONTROL_PLANE_BASE_URL。"""
    monkeypatch.setenv("CONTROL_PLANE_BASE_URL", "https://cp.example.invalid")
    monkeypatch.delenv("RTC_BRIDGE_CONTROL_PLANE_BASE_URL", raising=False)
    module = _load_supervisor()
    sup = module.BridgeSupervisor()
    assert sup.bridge._env["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"] == "https://cp.example.invalid"


def test_supervisor_does_not_override_explicit_bridge_base_url(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_BASE_URL", "https://alias.example.invalid")
    monkeypatch.setenv(
        "RTC_BRIDGE_CONTROL_PLANE_BASE_URL", "https://explicit.example.invalid"
    )
    module = _load_supervisor()
    sup = module.BridgeSupervisor()
    assert (
        sup.bridge._env["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"]
        == "https://explicit.example.invalid"
    )


def test_status_surfaces_tls_material_failure() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = ""
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    sup._tls_material = {"ok": False, "error": "RTC_BRIDGE_CLIENT_KEY_PEM"}
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")

    payload = sup.status()
    assert payload["tls_material"] == {"ok": False, "error": "RTC_BRIDGE_CLIENT_KEY_PEM"}
