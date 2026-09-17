"""契约：容器必须有可用的音频子系统，且真实 sidecar 的渲染进程日志必须可读。

背景（2026-09-16 线上事故，DeployId 015，CLS 实测）
--------------------------------------------------
容器原生日志：

    pulse_audio_context.cc:69   pulse server connect failed, please install pulseaudio
    pulse_audio_context.cc:1327 current context is not ready  GetDevices wait

其后 `EnterRoom` 的 26 分钟里**一行原生日志都没有**（对比启动阶段 50+ 行），桥侧权威上行
计数 `up rms=%.0f frames=%d`（backend/rtc_bridge/session.py:308，每 2s 一条）一条都没出现
⇒ 从没有一帧上行到达桥 ⇒ 手机侧 `user size` 恒为 1（真机与内置模拟机都是）
⇒ **手机与云端媒体面从来没共处一室**。

第二个缺陷同样致命：`cloudbridge/supervisor.py` 只给内置模拟机加了
`--enable-logging=stderr`，真实 sidecar 的渲染进程 JS 日志（`进房成功/进房失败 errCode=`、
`[PCM]`、`[UPRMS]`、`[STAT]`）既不进 stdout（CLS）也不在 `/status.sidecar.output_tail` 里
—— 排查时根本看不见对端在说什么。本文件把这两条一起守住。

为什么这里既有静态断言又有行为断言
----------------------------------
**静态断言（仓库形状）**：`Dockerfile` 的 apt 清单与构建期自证 RUN 是"镜像里装了什么"
的事实，本地没有 docker、无法构建镜像，所以只能核对文本形状——这**不是**在拿扫源码冒充
行为验证，真正的行为验证被放进了构建期（`audio_env.py --self-test`，起不来即构建失败）
与部署后的运行时（`[audio] pulseaudio ready` 一行日志）。

**行为断言**：`audio_env` 与 `supervisor` 的启动/失败分支可以在本机用替身真实跑出来
（见下面各用例），因此这些不做文本匹配。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"

sys.path.insert(0, str(CLOUDBRIDGE))

import audio_env  # noqa: E402
import supervisor as sup  # noqa: E402


# --- 1. 仓库形状：镜像里到底装了什么 ----------------------------------------


def _apt_install_tokens() -> list[str]:
    """Dockerfile 里 `RUN apt-get update && apt-get install -y ...` 这条**逻辑行**的词。

    先把以 `\\` 续行的物理行拼回逻辑行再分词：直接用 `[^\\n]*` 的正则去跨行匹配，
    由于续行组可以是零次，正则在第一行就会成功返回（贪心不会为了更长匹配而回溯），
    实测只拿到第一行——这种"看起来在检查、其实只看了三行"的断言比没有更危险。
    """
    tokens: list[str] = []
    pending = ""
    for raw in (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = pending + raw
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + " "
            continue
        pending = ""
        if "apt-get install" in line:
            tokens.extend(line.split())
    assert tokens, "Dockerfile 里找不到 apt-get install 段"
    return tokens


def test_dockerfile_apt_segment_installs_pulseaudio() -> None:
    """没有 pulseaudio，TRTC 的原生层根本连不上声音服务端（本次事故第一因）。"""
    packages = {token.strip("\\") for token in _apt_install_tokens()}
    assert "pulseaudio" in packages, (
        "镜像 apt 清单必须包含 pulseaudio（守护进程 + module-native-protocol-unix / "
        "module-null-sink / module-virtual-source 三个标准模块都来自这个包）"
    )
    # 不装 pactl 就没法在构建期清点设备，构建期自证会退化成"只看套接字"。
    assert "pulseaudio-utils" in packages, "构建期自证需要 pactl 清点 sink/source"


def test_dockerfile_verifies_the_audio_subsystem_during_build() -> None:
    """本机没有 docker ⇒ 用**构建期自证**代替"只能等线上才知道"。

    自证跑的是运行期同一份 argv（`audio_env.build_plan`），起不来/套接字不出现/
    设备清点为空都让构建失败，绝不产出"进程起得来但音频层是哑的"镜像。
    """
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "audio_env.py --self-test" in text, "缺少构建期音频自证 RUN"
    assert "COPY cloudbridge ./cloudbridge" in text


# --- 2. 启动计划：前台运行 + 显式设备 + 显式路径 -----------------------------


def _plan(tmp_path: Path, **kwargs) -> audio_env.AudioPlan:
    return audio_env.build_plan(tmp_path, which=lambda name: f"/usr/bin/{name}", **kwargs)


def test_plan_fails_closed_when_the_binary_is_missing(tmp_path: Path) -> None:
    """真正决定行为的是"找不到二进制"，因此这里断言抛错，而不是断言源码里有某行文字。"""
    with pytest.raises(audio_env.AudioSubsystemError) as excinfo:
        audio_env.build_plan(tmp_path, which=lambda name: None)
    assert "pulseaudio" in str(excinfo.value)


def test_plan_runs_in_foreground_so_logs_reach_cls(tmp_path: Path) -> None:
    """守护化（--daemonize）会把日志交给 syslog；容器里没有 syslog ⇒ 又变成看不见。

    本次事故的全部难点就是"看不见"，所以这一条不是风格偏好。
    """
    argv = _plan(tmp_path).argv
    assert "--daemonize=no" in argv
    assert "--log-target=stderr" in argv
    assert not any(a.startswith("--daemonize=yes") for a in argv)
    assert "--exit-idle-time=-1" in argv, "空闲自动退出会让 TRTC 后续连接直接失败"


def test_plan_loads_modules_explicitly_instead_of_the_distro_default(tmp_path: Path) -> None:
    """`-n` + 显式 -L：不依赖发行版 default.pa 的偶然行为（它会去碰 alsa/dbus）。

    顺序有意义：virtual-source 的 master 是 null-sink 的 monitor，null-sink 必须先加载。
    """
    argv = _plan(tmp_path).argv
    assert "-n" in argv
    loads = [a for a in argv if a.startswith("--load=")]
    assert loads, "必须显式加载模块"
    joined = " | ".join(loads)
    assert "module-native-protocol-unix" in joined, "没有它客户端连不上服务端"
    assert f"sink_name={audio_env.SINK_NAME}" in joined, "无头容器没有声卡，必须自己造 sink"
    assert audio_env.SOURCE_NAME in joined, "TRTC 的采集设备枚举需要一个真实存在的 source"
    assert loads.index(next(a for a in loads if "module-null-sink" in a)) < loads.index(
        next(a for a in loads if "module-virtual-source" in a)
    ), "virtual-source 的 master 是 null-sink 的 monitor，加载顺序不能反"


def test_plan_gives_the_null_sink_the_property_the_sdk_actually_reads(tmp_path: Path) -> None:
    """`device.form_factor` 必须有值 —— 它是 SDK 读取的**两个** device.* 属性之一。

    判据（离线取证，可复现）：对 libliteavsdk.so（12.5.705-beta.0，
    sha256 c5a6f1df…，与线上容器 pin 的 zip sha256 45da7b83… 一致）做全库可打印串扫描 +
    `.rodata → .text` 的 RIP 相对取址交叉引用，`device.*` 前缀里**只有**
    `device.description` 与 `device.form_factor` 被引用；`device.class` 零命中。
    所以这里**只**要求补 form_factor：`device.class=sound` / `device.icon_name` 无证据支持，
    加了也只是让镜像更像真机、并不能被 SDK 读到（不做的理由与被做的理由同等重要）。
    """
    loads = [a for a in _plan(tmp_path).argv if a.startswith("--load=")]
    sink_load = next(a for a in loads if "module-null-sink" in a)
    source_load = next(a for a in loads if "module-virtual-source" in a)
    assert sink_load == (
        f"--load=module-null-sink sink_name={audio_env.SINK_NAME} "
        'sink_properties="device.description=JaxNullSink device.form_factor=speaker"'
    ), "属性必须整串用双引号包住、内部以空白分条（019 的逗号写法根本产生不了第二个属性）"
    assert source_load == (
        f"--load=module-virtual-source source_name={audio_env.SOURCE_NAME} "
        "master=jax_null.monitor "
        'source_properties="device.description=JaxNullMic device.form_factor=microphone"'
    )
    joined = " | ".join(loads)
    assert "device.class=" not in joined, "device.class 在全库里 0 命中 ⇒ 不按'像真机'去补"
    assert "device.icon_name=" not in joined


def test_plan_quotes_the_properties_so_they_survive_both_parsers(tmp_path: Path) -> None:
    """019 的教训固化进断言：逗号分隔**不会**产生第二个属性，而漏引号**更糟**。

    PA 的两层解析器都**只以空白分隔**：
      · src/pulsecore/modargs.c:107-142 的 parse()：isspace() 分 `key=value`，逗号只是
        取值里的普通字符；
      · src/pulse/proplist.c:450-543 的 pa_proplist_from_string()：同样只认 isspace()。
    所以 `a=1,b=2` 只会把 description 变成
    `JaxNullSink,device.form_factor=speaker`（属性没落上）；而 `a=1 b=2` 在**不加**外层
    引号时，`device.form_factor=speaker` 会被当成**独立的模块参数**，它不在
    module-null-sink 的白名单里（module-null-sink.c:80-90）⇒ pa_modargs_new 返回 NULL
    （modargs.c:66-77 未知 key 即 fail）⇒ 模块加载失败 ⇒ 守护进程退出、容器崩溃重启。
    这个双重引号写法是上游自己的用法：v16.1 src/daemon/default.pa.in:116 就是
    `sink_properties="device.description='RTP Multicast Sink'"`；而 `--load=` 的值由
    cmdline.c:226-228 原样拼成 `load-module %s\n` 后走同一套配置文件解析器。
    """
    load = next(a for a in _plan(tmp_path).argv
                if a.startswith("--load=module-null-sink"))
    value = load.split("sink_properties=", 1)[1]
    assert value.startswith('"') and value.endswith('"'), \
        "整串属性必须被 modargs 层的双引号包住，否则第二个 key 会脱离 sink_properties"
    inner = value[1:-1]
    assert "," not in inner, "逗号不是属性分隔符（019 就死在这）"
    assert inner.split() == ["device.description=JaxNullSink",
                             "device.form_factor=speaker"], "proplist 层靠空白把属性分条"


def test_plan_pins_every_path_libpulse_could_guess(tmp_path: Path) -> None:
    """容器里没有 user session ⇒ XDG_RUNTIME_DIR 默认不存在，两边会各找一个不存在的目录。"""
    plan = _plan(tmp_path)
    assert plan.env["XDG_RUNTIME_DIR"] == str(tmp_path)
    assert plan.env["PULSE_RUNTIME_PATH"] == str(tmp_path / "pulse")
    assert plan.env["PULSE_SERVER"] == f"unix:{tmp_path / 'pulse' / 'native'}"
    assert plan.socket_path == tmp_path / "pulse" / "native"
    assert plan.server == plan.env["PULSE_SERVER"]


# --- 3. 就绪判定：套接字真的出现才算 ----------------------------------------


class _FakeSocket:
    """只带 `is_socket()` 的替身：本机 Windows 的 Python 没有 AF_UNIX，造不出真套接字。"""

    def __init__(self, ready_after: int) -> None:
        self.calls = 0
        self._ready_after = ready_after

    def is_socket(self) -> bool:
        self.calls += 1
        return self.calls > self._ready_after


def test_wait_for_socket_returns_true_once_it_appears() -> None:
    slept: list[float] = []
    assert audio_env.wait_for_socket(_FakeSocket(ready_after=2), timeout=5,
                                     sleep=slept.append) is True
    assert slept, "未就绪时必须轮询等待，不能一次判死"


def test_wait_for_socket_gives_up_as_soon_as_the_process_is_gone() -> None:
    """进程已死就立刻放弃：空等满超时会让容器在崩溃时多挂 20s 才吐死因。"""
    slept: list[float] = []
    assert audio_env.wait_for_socket(_FakeSocket(ready_after=99), timeout=30,
                                     sleep=slept.append, alive=lambda: False) is False
    assert slept == [], "进程已死时不得再 sleep 等超时"


def test_wait_for_socket_times_out_on_a_missing_path(tmp_path: Path) -> None:
    slept: list[float] = []
    assert audio_env.wait_for_socket(tmp_path / "never-there", timeout=0.0,
                                     sleep=slept.append) is False


# --- 4. supervisor：fail-closed 启动音频，且必须先于 sidecar ----------------


class _StubChild:
    def __init__(self, *, alive: bool = True, exit_code: int | None = None,
                 name: str = "child") -> None:
        self.name = name
        self._alive = alive
        self.exit_code = exit_code
        self.starts = 1
        self.pid = 4242
        self.liveness = True
        self.tail: list[str] = []
        self._env: dict[str, str] = {}

    def alive(self) -> bool:
        return self._alive

    def reap(self):
        return self.exit_code

    def describe(self) -> dict:
        return {"alive": self._alive, "pid": self.pid, "starts": self.starts,
                "exit_code": self.exit_code, "output_tail": list(self.tail)}

    def signal(self, _sig) -> None:
        self._alive = False


class _RecordingChild(_StubChild):
    """记录启动过程，用来断言"音频先于 sidecar"。"""

    def __init__(self, name: str, argv, cwd, extra_env, **kwargs) -> None:
        super().__init__(name=name)
        self.argv = list(argv)
        self.env = dict(extra_env)
        self._env = dict(extra_env)
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        self.started = True


@pytest.fixture()
def _restore_environ():
    """`_start_audio` 成功时会 os.environ.update(...)，别把路径串进别的用例。"""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def _bare_supervisor() -> sup.BridgeSupervisor:
    s = sup.BridgeSupervisor.__new__(sup.BridgeSupervisor)
    s.shutting_down = False
    s.sidecar_enabled = True
    s.sim_enabled = False
    s.crash_grace_s = 0
    s.bridge = _StubChild(alive=True)
    s.sidecar = _StubChild(alive=True)
    s.audio = None
    s._audio_plan = None
    s._audio_status = {"ok": False, "state": "not-started", "error": ""}
    s.audio_runtime_dir = str(Path("/tmp/jax-audio"))
    s.audio_ready_timeout_s = 0.01
    return s


def test_audio_starts_before_the_sidecar(monkeypatch, tmp_path: Path) -> None:
    """sidecar 一进房就要用音频设备——音频子系统必须先就绪。"""
    order: list[str] = []
    s = _bare_supervisor()
    s._materialize_bridge_tls = lambda: order.append("tls")
    s._start_audio = lambda: order.append("audio")
    s.bridge = _RecordingChild("rtc_bridge", [], tmp_path, {})
    s.sidecar = _RecordingChild("sidecar", [], tmp_path, {})
    s.bridge.start = lambda: order.append("bridge")
    s.sidecar.start = lambda: order.append("sidecar")

    s.start()

    assert order == ["tls", "audio", "bridge", "sidecar"]


def _boom(*_args, **_kwargs):
    raise audio_env.AudioSubsystemError("找不到 pulseaudio")


def test_audio_startup_fails_closed_without_the_binary(monkeypatch, tmp_path: Path) -> None:
    """起不来就必须让容器退出：带着哑音频层跑 = 本次事故（进房永远完不成且毫无日志）。"""
    s = _bare_supervisor()
    monkeypatch.setattr(audio_env, "build_plan", _boom)

    with pytest.raises(SystemExit) as excinfo:
        s._start_audio()

    assert excinfo.value.code == 1
    assert s._audio_status["ok"] is False
    assert s._audio_status["state"] == "unavailable"


def test_audio_startup_fails_closed_when_the_socket_never_appears(monkeypatch,
                                                                  tmp_path: Path) -> None:
    """进程活着但没有套接字 = TRTC 的 `GetDevices wait`，必须判死而不是放行。"""
    s = _bare_supervisor()
    plan = _plan(tmp_path)
    monkeypatch.setattr(audio_env, "build_plan", lambda *a, **k: plan)
    monkeypatch.setattr(audio_env, "wait_for_socket", lambda *a, **k: False)
    monkeypatch.setattr(sup, "Child", _RecordingChild)

    with pytest.raises(SystemExit) as excinfo:
        s._start_audio()

    assert excinfo.value.code == 1
    assert s._audio_status["state"] == "not_ready"
    assert s.audio is not None and s.audio.started, "诊断信息要留住：进程确实被拉起过"


def test_audio_failure_is_reported_on_stdout(caplog, monkeypatch, tmp_path: Path) -> None:
    """fail-closed 必须**可见**：一条 `[audio] FATAL:` 落在 stdout（进 CLS）才算数。"""
    s = _bare_supervisor()
    monkeypatch.setattr(audio_env, "build_plan", _boom)

    with caplog.at_level("ERROR", logger="jax-voice-bridge"):
        with pytest.raises(SystemExit):
            s._start_audio()

    assert any("[audio] FATAL" in r.getMessage() for r in caplog.records), caplog.text


def test_audio_environment_reaches_the_sidecar(monkeypatch, tmp_path: Path,
                                               _restore_environ) -> None:
    """sidecar 由 xvfb-run 派生，多一跳最容易丢变量 ⇒ 必须显式注入它的子进程 env。"""
    s = _bare_supervisor()
    plan = _plan(tmp_path)
    s.sidecar = _RecordingChild("sidecar", [], tmp_path, {})
    monkeypatch.setattr(audio_env, "build_plan", lambda *a, **k: plan)
    monkeypatch.setattr(audio_env, "wait_for_socket", lambda *a, **k: True)
    monkeypatch.setattr(sup, "Child", _RecordingChild)

    s._start_audio()

    assert s._audio_status["ok"] is True
    assert s.sidecar._env["PULSE_SERVER"] == plan.server
    assert s.sidecar._env["XDG_RUNTIME_DIR"] == str(tmp_path)
    # pulseaudio 自己也必须被监督：它死掉要整体退出，不能静默退化成"哑音频层"。
    assert s.audio is not None and s.audio.name == "pulseaudio"


def test_dead_audio_subsystem_is_fatal() -> None:
    s = _bare_supervisor()
    s.audio = _StubChild(alive=False, exit_code=1, name="pulseaudio")

    assert s._first_dead() is s.audio, "pulseaudio 退出必须整体退出，由平台重启"


def test_audio_is_skipped_only_when_the_sidecar_is_disabled() -> None:
    """没有 TRTC 对端时不需要音频层；此时不得凭空失败，但状态要如实标注 skipped。"""
    s = _bare_supervisor()
    s.sidecar_enabled = False

    s._start_audio()

    assert s._audio_status == {"ok": True, "state": "skipped", "error": ""}
    assert s.audio is None


def test_status_reports_audio_state() -> None:
    s = _bare_supervisor()
    s.bridge_health_url = "http://127.0.0.1:19093/health"
    s.sign_url = ""
    s.device_id = "jax-cloud-bridge"
    s.sidecar_enabled = True
    s._audio_status = {"ok": True, "state": "ready", "sink": "jax_null", "error": ""}
    object.__setattr__(s, "_probe_bridge_health", lambda: "ok")

    payload = s.status()

    assert payload["audio"]["state"] == "ready"
    assert payload["audio"]["sink"] == "jax_null"


# --- 5. 手机模拟器：同容器同 TRTC，同样要拿到音频环境 -----------------------


def test_audio_environment_reaches_the_phone_simulator(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class _Capture(_RecordingChild):
        def __init__(self, name, argv, cwd, extra_env, **kwargs):
            super().__init__(name, argv, cwd, extra_env, **kwargs)
            captured["env"] = dict(extra_env)
            captured["argv"] = list(argv)

    s = sup.BridgeSupervisor()
    s.sim_enabled = True
    s.sign_url = "https://control-plane.example"
    s.sim_log_dir = tmp_path / "logs"
    s._audio_plan = _plan(tmp_path)
    monkeypatch.setattr(
        sup.sim_provision, "resolve_sim_device",
        lambda **kwargs: sup.sim_provision.SimDevice(
            device_id="dev-9", credential_token="dev-9.secret", expires_at=""
        ),
    )
    monkeypatch.setattr(sup.sim_phone, "ensure_prompt_wav",
                        lambda *a, **k: tmp_path / "p.wav")
    monkeypatch.setattr(sup, "Child", _Capture)

    s._start_sim_phone()

    assert captured["env"]["PULSE_SERVER"] == s._audio_plan.server
    assert captured["env"]["XDG_RUNTIME_DIR"] == str(tmp_path)
    # 凭证仍然只走 env，不得因这次改动泄进 argv。
    assert not any("dev-9.secret" in str(a) for a in captured.get("argv", []))


# --- 6. 观测：真实 sidecar 的渲染进程日志必须能出来 -------------------------


def test_real_sidecar_enables_renderer_logging(monkeypatch) -> None:
    """真实 sidecar 的渲染进程日志既不进 stdout 也不在 /status 里 —— 最大的观测障碍。

    这是行为断言（读的是**构造出来的 argv**），不是文本匹配：`测试` 通过
    `BridgeSupervisor()` 真实构造 sidecar 子进程并检查它的启动参数。
    """
    s = sup.BridgeSupervisor()
    assert "--enable-logging=stderr" in s.sidecar.argv, (
        "真实 sidecar 必须与内置模拟机一样把渲染进程 console 写进 stderr → stdout(CLS)"
    )
    assert "--role=sidecar" in s.sidecar.argv
    assert not any("--device=" in a for a in s.sidecar.argv), "role=sidecar 带 --device 会自身 fail-closed"


def test_sidecar_tail_window_is_widened_for_renderer_logs() -> None:
    """打开渲染进程日志后行数陡增，固定 120 行会把"进房失败 errCode="挤出 tail。"""
    s = sup.BridgeSupervisor()
    assert s.sidecar.tail_lines > sup.Child.TAIL_LINES
    assert sup.BridgeSupervisor().bridge.tail_lines == sup.Child.TAIL_LINES


# --- 7. 构建期自证本身必须真的会拦人 ---------------------------------------


class _FakeProc:
    def __init__(self) -> None:
        self.killed = False

    def poll(self):
        return None

    def send_signal(self, _sig) -> None:
        self.killed = True

    def wait(self, timeout=None):  # noqa: ARG002 - 与 Popen 同形
        self.killed = True
        return 0

    def kill(self) -> None:
        self.killed = True


def _pactl_run(stdout_by_kind: dict[str, str]):
    def _run(cmd, **kwargs):  # noqa: ARG001
        kind = cmd[-1]
        text = stdout_by_kind.get(kind, "")
        return subprocess.CompletedProcess(cmd, 0, text, "")
    return _run


# `pactl list <kind>`（长格式）。同一份文本既是 `_pactl_lines` 的输入、也是长格式解析器的
# 输入（替身按 `cmd[-1]` 分发），所以夹具必须**让设备存在与属性落上两个判据同时成立**。
PACTL_SELFTEST_SINKS_OK = (
    "Sink #0\n"
    "\tState: SUSPENDED\n"
    "\tName: jax_null\n"
    "\tDescription: JaxNullSink\n"
    "\tFlags: DECIBEL_VOLUME LATENCY SET_FORMATS \n"
    "\tProperties:\n"
    '\t\tdevice.description = "JaxNullSink"\n'
    '\t\tdevice.form_factor = "speaker"\n'
)
PACTL_SELFTEST_SOURCES_OK = (
    "Source #0\n"
    "\tState: SUSPENDED\n"
    "\tName: jax_null.monitor\n"
    "\tFlags: DECIBEL_VOLUME LATENCY \n"
    "\tProperties:\n"
    '\t\tdevice.description = "Monitor of JaxNullSink"\n'
    '\t\tdevice.class = "monitor"\n'
    "Source #1\n"
    "\tState: SUSPENDED\n"
    "\tName: jax_null.mic\n"
    "\tFlags: DECIBEL_VOLUME LATENCY \n"
    "\tProperties:\n"
    '\t\tdevice.description = "JaxNullMic"\n'
    '\t\tdevice.form_factor = "microphone"\n'
)
# 019 实际发出去的那一版观测形态：逗号没有产生第二个属性，form_factor 根本没落上设备
# （它被并进了 description 的取值里）。
PACTL_SELFTEST_SINKS_COMMA_FORM = (
    "Sink #0\n"
    "\tState: SUSPENDED\n"
    "\tName: jax_null\n"
    "\tFlags: DECIBEL_VOLUME LATENCY SET_FORMATS \n"
    "\tProperties:\n"
    '\t\tdevice.description = "JaxNullSink,device.form_factor=speaker"\n'
)


def _selftest(monkeypatch, tmp_path: Path, *, sinks: str, sources: str, which_ok: bool):
    monkeypatch.setattr(audio_env, "wait_for_socket", lambda *a, **k: True)
    return audio_env.selftest(
        runtime_dir=tmp_path,
        popen=lambda *a, **k: _FakeProc(),
        which=lambda name: (f"/usr/bin/{name}" if which_ok else None),
        run=_pactl_run({"sinks": sinks, "sources": sources}),
        sleep=lambda _s: None,
        log=lambda _m: None,
    )


def test_build_selftest_passes_when_sink_and_source_exist(monkeypatch, tmp_path: Path) -> None:
    code = _selftest(
        monkeypatch, tmp_path,
        sinks=PACTL_SELFTEST_SINKS_OK,
        sources=PACTL_SELFTEST_SOURCES_OK,
        which_ok=True,
    )
    assert code == 0


def test_build_selftest_fails_when_the_property_did_not_land(monkeypatch, tmp_path: Path) -> None:
    """设备在、套接字就绪，但属性没落上 —— 019 正是这样"全绿"发出去的。

    这一关是 2026-09-17 补的：原来的自证只问"设备在不在"，而 SDK 读的是
    `device.form_factor` **这个键本身**；逗号写法把它并进了 description 的取值里，
    设备照样存在 ⇒ 自证全绿 ⇒ 镜像照发，可 SDK 那边 form_factor 是空的。
    """
    assert _selftest(monkeypatch, tmp_path,
                     sinks=PACTL_SELFTEST_SINKS_COMMA_FORM,
                     sources=PACTL_SELFTEST_SOURCES_OK,
                     which_ok=True) == 1, "sink 侧属性没落上必须判失败"
    assert _selftest(monkeypatch, tmp_path,
                     sinks=PACTL_SELFTEST_SINKS_OK,
                     sources=PACTL_SELFTEST_SOURCES_OK.replace(
                         'device.form_factor = "microphone"',
                         'device.form_factor = "microphone,unused=x"'),
                     which_ok=True) == 1, "source 侧属性没落上同样必须判失败（只补 sink 不算完）"


def test_build_selftest_fails_when_devices_are_missing(monkeypatch, tmp_path: Path) -> None:
    """只验"进程起来了"是不够的：设备表为空时 TRTC 依旧 GetDevices wait。"""
    assert _selftest(monkeypatch, tmp_path, sinks="", sources="", which_ok=True) == 1
    assert _selftest(monkeypatch, tmp_path,
                     sinks="0\tjax_null\tmodule-null-sink\t...\n", sources="",
                     which_ok=True) == 1


def test_build_selftest_fails_when_pactl_is_missing(monkeypatch, tmp_path: Path) -> None:
    """pactl 缺席就清点不了设备 —— 那等于把这条自证退化成空转，必须判失败。"""
    assert _selftest(monkeypatch, tmp_path, sinks="", sources="", which_ok=False) == 1


def test_build_selftest_fails_closed_without_the_binary(tmp_path: Path) -> None:
    code = audio_env.selftest(runtime_dir=tmp_path, which=lambda name: None,
                              log=lambda _m: None)
    assert code == 1
