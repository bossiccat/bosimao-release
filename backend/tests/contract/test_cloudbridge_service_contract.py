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
    sup.sim_enabled = False
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
    sup.sim_enabled = False
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
    sup.sim_enabled = False
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
    sup.sim_enabled = False
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
    sup.sim_enabled = False
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
    --device 只属于 role=phone，因此**对端**不得传——但手机模拟器必须传。"""
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    sidecar_block = text.split("self.sidecar = Child(", 1)[1].split(")", 1)[0]
    assert "--device=" not in sidecar_block, "sidecar 启动参数里不得出现 --device="
    sim_block = text.split("self.sim_phone = Child(", 1)[1].split("liveness=False", 1)[0]
    assert "--device=" in sim_block, "手机模拟器必须以 --device 指定自己的 device_id"


def test_bridge_health_port_stays_loopback_only() -> None:
    """rtc_bridge 的 19092/19093 只在容器内可达，不对外暴露。"""
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    assert "ws://127.0.0.1:19092" in text
    assert "127.0.0.1:19093" in text


# --- 6. 云端手机模拟（无真机的端到端验证）----------------------------------


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


def test_sim_phone_is_not_a_liveness_child() -> None:
    """模拟器跑完即退出是预期：绝不能因此把容器判死（否则会无限重启）。"""
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = False
    sup.crash_grace_s = 0
    sup.sim_enabled = False
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    dead_sim = _StubChild(alive=False, exit_code=0, name="sim-phone")
    dead_sim.liveness = False
    sup.sim_phone = dead_sim

    # 一次性子进程已退出，但 liveness 子进程都健康 → 不得抛 SystemExit
    assert sup._first_dead() is None


def test_sim_phone_launches_in_the_phone_role_with_prompt_and_recording() -> None:
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    for flag in ("--role=phone", "--wav=", "--out-wav=", "--hold=", "--device="):
        assert flag in text, f"手机模拟启动参数缺少 {flag}"
    assert "liveness=False" in text, "模拟器必须标记为非存活子进程"


def test_image_can_synthesise_the_prompt_audio_in_cloud() -> None:
    """提示音必须能在云端生成：真实中文语音（edge-tts）转 16k wav（ffmpeg）。

    用音调代替语音测不出链路真伪——模型只对语音产生有意义的回复。
    """
    dockerfile = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "ffmpeg" in dockerfile
    assert "edge-tts" in dockerfile


def test_status_reports_simulation_when_enabled() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.sim_enabled = False
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = ""
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.sim_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")
    import threading as _threading
    sup._sim_lock = _threading.Lock()
    sim = _load_sim_phone()
    sup.sim_metrics = sim.parse_phone_log(REAL_PHONE_LOG)

    payload = sup.status()
    assert payload["simulation"]["state"] == "replied"
    assert payload["simulation"]["reply_bytes"] == 96640
    # 对外报出的口径也要一起守住：状态端点是别人读数的唯一入口。
    assert payload["simulation"]["utterance_ms"] == 3120
    assert payload["simulation"]["speech_ms"] == 3120


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
    sup.sim_enabled = False
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
