"""契约：`/status.audio` 必须区分「计划里的设备」与「PA 里实测到的设备」。

背景（2026-09-16 线上事故）
--------------------------
容器实跑（同一分钟内的两份日志）：

    # supervisor 自报（来自启动计划）
    /status.audio = {"ok": true, "state": "ready", "sink": "jax_null", "source": "jax_null.mic"}

    # sidecar 侧（TRTC 原生日志 + 我们自己的计数）
    [I][…][audio_player_safe_wrapper.cc:384] Player error player device list is empty … （io_source:player）
    [W][…][audio_event_dispatcher.cc:272] OnWarning [code:1202|message:player device list is empty]
    [I][…][io_working_status_printer.cc:107] Within 40000 ms, kPlayout produced 0 ms data, callback count is 0
    [I][…][rtc_audio_jitter_buffer_v2.cc:484] PacketBuffer is full … io_last_read_frame_ticks: -1
    [STAT] up=0帧/0KB down=0帧/0KB ws=true      ← 连续 70 秒

两份日志互相矛盾，而这个矛盾**无法从 `/status` 判开** —— 因为 `audio.sink` / `audio.source`
是**启动计划的值**（"我们打算造什么"），不是实测（"真的造出来了什么"）。
`inspect_devices()` 早就在仓库里（构建期自证用它），运行期却一次都没调用过。

本文件守住三件事：
1. 运行期必须**实测**清点，并如实报出来（`audio.devices`），计划值与实测值永不复用同一个键；
2. `playout_ok` 必须三态（True/False/None）：pactl 失败 = **不知道**，既不是"有设备"也不是
   "没有设备" —— 把工具故障误读成"音频子系统是哑的"会把下一次排查再次带偏；
3. 清点**不得影响启动/退出语义**（fail-open）：它是观测，不是门禁。

与 `test_bridge_audio_subsystem_contract.py` 的分工：那个文件守构建期自证与启动顺序，
本文件只守**运行期实测口径**，两者互不替代。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"

sys.path.insert(0, str(CLOUDBRIDGE))

import audio_env  # noqa: E402
import supervisor as sup  # noqa: E402


# --- 1. 纯解析：默认设备与客户端清单 ---------------------------------------

# 真实 `pactl info` 的在容器里的形态（Default Sink 来自 module-null-sink 的 sink_name）。
PACTL_INFO = (
    "Server String: /tmp/jax-audio/pulse/native\n"
    "Library Protocol Version: 35\n"
    "Server Protocol Version: 35\n"
    "Default Sink: jax_null\n"
    "Default Source: jax_null.monitor\n"
    "Server Name: pulseaudio\n"
)


def test_parse_server_defaults_reads_real_pactl_info() -> None:
    defaults = audio_env.parse_server_defaults(PACTL_INFO)
    assert defaults["default_sink"] == "jax_null"
    assert defaults["default_source"] == "jax_null.monitor"


def test_parse_server_defaults_does_not_guess_when_fields_are_absent() -> None:
    """字段缺失（PA 里没有默认设备）= None，绝不用计划里的名字顶替。"""
    defaults = audio_env.parse_server_defaults("Server Name: pulseaudio\n")
    assert defaults == {"default_sink": None, "default_source": None}
    assert audio_env.parse_server_defaults(None) == {"default_sink": None, "default_source": None}


def test_parse_client_names_is_the_only_direct_evidence_of_which_server_was_reached() -> None:
    """`pactl list short clients` 第 2 列是应用名；liteav 在不在，决定"连对实例没有"。"""
    names = audio_env.parse_client_names([
        "12\tliteav\tmodule-native-protocol-unix\ts16le 2ch 48000Hz\tRUNNING",
        "13\tpactl\tmodule-native-protocol-unix\ts16le 2ch 48000Hz\tRUNNING",
    ])
    assert names == ["liteav", "pactl"]


def test_parse_client_names_keeps_unknown_separate_from_empty() -> None:
    """None = 没清点成；[] = 清点成了但一个客户端都没有。两者不可混。"""
    assert audio_env.parse_client_names(None) is None
    assert audio_env.parse_client_names([]) == []


# --- 1b. 长格式清点：null-sink 与真实声卡的**不对称**是"被筛掉什么"的唯一判据 ---

# `pactl list sinks` 里 null-sink 的**观测形态样本**：没有 Ports 段，属性表里只有我们
# 显式写进去的 device.description（见 audio_env 里 module-null-sink 的 sink_properties）。
# 注意：样本只钉**解析器行为**——线上那个 sink 到底有没有端口、带不带 device.class，
# 正是要拿这份观测去问的问题，不能在这里替它作答。
PACTL_LIST_SINKS_NULL = (
    "Sink #0\n"
    "\tState: SUSPENDED\n"
    "\tName: jax_null\n"
    "\tDescription: JaxNullSink\n"
    "\tDriver: module-null-sink.c\n"
    "\tSample Specification: s16le 2ch 44100Hz\n"
    "\tChannel Map: front-left,front-right\n"
    "\tOwner Module: 25\n"
    "\tMute: no\n"
    "\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB\n"
    "\t        balance 0.00\n"
    "\tBase Volume: 65536 / 100% / 0.00 dB\n"
    "\tMonitor Source: jax_null.monitor\n"
    "\tLatency: 0 usec, configured 0 usec\n"
    "\tFlags: DECIBEL_VOLUME LATENCY SET_FORMATS \n"
    "\tProperties:\n"
    '\t\tdevice.description = "JaxNullSink"\n'
    "\tFormats:\n"
    "\t\tpcm\n"
)

# 对照：真实声卡（用来证明解析器不是"总会报空"）。
PACTL_LIST_SINKS_HW = (
    "Sink #1\n"
    "\tName: alsa_output.pci-0000_00_1f.3.analog-stereo\n"
    "\tDescription: Built-in Audio Analog Stereo\n"
    "\tFlags: HARDWARE HW_MUTE_CTRL DECIBEL_VOLUME LATENCY \n"
    "\tProperties:\n"
    '\t\talsa.card = "0"\n'
    '\t\tdevice.description = "Built-in Audio Analog Stereo"\n'
    '\t\tdevice.form_factor = "internal"\n'
    '\t\tdevice.class = "sound"\n'
    "\tPorts:\n"
    "\t\tanalog-output-speaker: Speakers (priority 10000, latency offset 0 usec, available: unknown)\n"
    "\t\t\tproperties:\n"
    '\t\t\t\tdevice.icon_name = "audio-speakers"\n'
    "\t\tanalog-output-headphones: Headphones (priority 9900, latency offset 0 usec, available: no)\n"
    "\tActive Port: analog-output-speaker\n"
    "\tFormats:\n"
    "\t\tpcm\n"
)


def test_parse_device_details_reports_ports_and_prop_keys_of_our_own_sink() -> None:
    """端口与属性键必须被摊平报出来 —— 这是"SDK 为什么筛掉它"的唯一线上判据。"""
    details = audio_env.parse_device_details(PACTL_LIST_SINKS_NULL)
    assert details is not None and len(details) == 1
    d = details[0]
    assert d["name"] == "jax_null"
    assert d["ports"] == [], "没有 Ports 段 ⇒ 空端口（不是解析失败，也不是'不知道'）"
    assert d["props"] == ["device.description"], "属性键里没有 device.class / device.form_factor"
    assert isinstance(d["flags"], list) and d["flags"], "Flags 必须被解析出来（线上要判 HARDWARE）"
    assert "DECIBEL_VOLUME" in d["flags"]


def test_parse_device_details_reads_real_ports_and_props() -> None:
    """真实声卡必须解析出端口与 form_factor/class —— 否则上面那条空端口断言是假绿。"""
    d = audio_env.parse_device_details(PACTL_LIST_SINKS_HW)[0]
    assert d["name"] == "alsa_output.pci-0000_00_1f.3.analog-stereo"
    assert d["ports"] == ["analog-output-speaker", "analog-output-headphones"]
    assert "device.form_factor" in d["props"] and "device.class" in d["props"]
    assert "HARDWARE" in d["flags"]
    # 端口**内部**那层 properties 不得污染属性键清单（层级必须钉死两格）。
    assert "device.icon_name" not in d["props"]


def test_parse_device_details_keeps_unknown_separate_from_empty() -> None:
    assert audio_env.parse_device_details(None) is None, "没清点成 ≠ 没有设备"
    assert audio_env.parse_device_details("") == []


def test_inspect_devices_carries_long_format_details() -> None:
    """`pactl list sinks`（长）与 `pactl list short sinks`（短）末参同名，键必须区分。"""
    report = audio_env.inspect_devices(
        env={}, sink=audio_env.SINK_NAME, which=lambda name: f"/usr/bin/{name}",
        run=_run_by_subcommand({
            "list short sinks": "0\tjax_null\tmodule-null-sink\ts16le 2ch 44100Hz\tSUSPENDED\n",
            "list short sources": "1\tjax_null.mic\tmodule-virtual-source\ts16le 2ch 44100Hz\n",
            "list short clients": "12\tliteav\tmodule-native-protocol-unix\ts16le 2ch 48000Hz\tRUNNING\n",
            "list sinks": PACTL_LIST_SINKS_NULL,
            "list sources": "Source #1\n\tName: jax_null.mic\n\tPorts:\n",
            "info": PACTL_INFO,
        }),
    )
    assert report["sink_details"][0]["name"] == "jax_null"
    assert report["sink_details"][0]["ports"] == []
    assert report["source_details"][0]["name"] == "jax_null.mic"


# --- 2. inspect_devices：probed 与"设备为空"是两件不同的事 -------------------


def _run_by_subcommand(answers: dict[str, str], *, returncode: int = 0):
    def _run(cmd, **kwargs):  # noqa: ARG001
        # 先按完整子命令（`list short sinks` / `list sinks` / `info`）查，
        # 再退回最后一个参数（`sinks`）——长短两种格式的**末参相同**，
        # 不区分就会把 short 格式喂给长格式解析器（那是这版最容易犯的错）。
        key = " ".join(cmd[1:])
        out = answers.get(key, answers.get(cmd[-1], ""))
        return subprocess.CompletedProcess(cmd, returncode, out, "")
    return _run


def test_inspect_devices_reports_devices_clients_and_defaults() -> None:
    report = audio_env.inspect_devices(
        env={}, sink=audio_env.SINK_NAME,
        which=lambda name: f"/usr/bin/{name}",
        run=_run_by_subcommand({
            "sinks": "0\tjax_null\tmodule-null-sink\ts16le 2ch 44100Hz\tSUSPENDED\n",
            "sources": ("1\tjax_null.monitor\tmodule-null-sink\tmonitor\n"
                        "2\tjax_null.mic\tmodule-virtual-source\ts16le 2ch 44100Hz\n"),
            "clients": "12\tliteav\tmodule-native-protocol-unix\ts16le 2ch 48000Hz\tRUNNING\n",
            "info": PACTL_INFO,
        }),
    )
    assert report["probed"] is True
    assert report["sink_present"] is True
    assert report["source_present"] is True
    assert report["default_sink"] == "jax_null"
    assert report["clients"] == ["liteav"]


def test_inspect_devices_without_pactl_is_unknown_not_empty() -> None:
    """pactl 缺席 ⇒ probed=False；绝不能表现成"清点成功、设备为空"。"""
    report = audio_env.inspect_devices(env={}, sink=audio_env.SINK_NAME,
                                       which=lambda _name: None, run=_run_by_subcommand({}))
    assert report["probed"] is False
    assert report["sinks"] is None and report["sources"] is None
    assert report["sink_present"] is False


def test_inspect_devices_when_pactl_fails_is_unknown_not_empty() -> None:
    report = audio_env.inspect_devices(
        env={}, sink=audio_env.SINK_NAME, which=lambda name: f"/usr/bin/{name}",
        run=_run_by_subcommand({"sinks": "", "sources": ""}, returncode=1),
    )
    assert report["probed"] is False, "命令返回非零 = 没清点成，不是'设备为空'"


# --- 3. summarize_devices：playout_ok 的三态 ---------------------------------


def test_summarize_devices_playout_ok_true_only_when_probed_and_present() -> None:
    summary = audio_env.summarize_devices({
        "probed": True, "sinks": ["0\tjax_null\t…"], "sources": ["1\tjax_null.mic\t…"],
        "clients": ["liteav"], "sink_present": True, "source_present": True,
        "default_sink": "jax_null", "default_source": "jax_null.monitor",
    })
    assert summary["playout_ok"] is True
    assert summary["sink_count"] == 1 and summary["source_count"] == 1
    assert summary["client_names"] == ["liteav"]


def test_summarize_devices_playout_ok_false_is_the_incident_condition() -> None:
    """清点成功但一个 sink 都没有 ⇒ 远端音频帧永不回调、上行恒为 0。"""
    summary = audio_env.summarize_devices({
        "probed": True, "sinks": [], "sources": [], "clients": [],
        "sink_present": False, "source_present": False,
        "default_sink": None, "default_source": None,
    })
    assert summary["playout_ok"] is False
    assert summary["sink_count"] == 0
    assert summary["default_sink"] is None


def test_summarize_devices_playout_ok_none_when_not_probed() -> None:
    """没清点成 ⇒ None（不知道）。这一条防的是"把工具故障读成设备缺失"。"""
    summary = audio_env.summarize_devices(None, error="PermissionError: boom")
    assert summary["playout_ok"] is None
    assert summary["probed"] is False
    assert summary["sink_count"] is None
    assert "boom" in summary["error"]


def test_summarize_devices_passes_the_asymmetry_through_to_status() -> None:
    """`playout_ok=True` 但 SDK 仍说设备列表为空时，端口/属性键必须已经在 /status 里。"""
    details = audio_env.parse_device_details(PACTL_LIST_SINKS_NULL)
    summary = audio_env.summarize_devices({
        "probed": True, "sinks": ["0\tjax_null\t…"], "sources": ["1\tjax_null.mic\t…"],
        "clients": ["liteav"], "sink_present": True, "source_present": True,
        "default_sink": "jax_null", "default_source": "jax_null.monitor",
        "sink_details": details, "source_details": [],
    })
    assert summary["playout_ok"] is True
    assert summary["sink_details"][0]["ports"] == []
    assert summary["sink_details"][0]["props"] == ["device.description"]
    assert summary["source_details"] == []


# --- 4. supervisor：运行期实测进 /status，且不影响启动语义 ---------------------


class _StubChild:
    def __init__(self, *, name: str = "child", **kwargs) -> None:  # noqa: ARG002
        self.name = name
        self.argv = list(kwargs.get("argv", []) or [])
        self._env: dict[str, str] = {}
        self.liveness = True
        self.exit_code = None
        self.pid = 4242
        self.tail: list[str] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def alive(self) -> bool:
        return True

    def reap(self):
        return None

    def describe(self) -> dict:
        return {"alive": True, "pid": self.pid, "starts": 1, "exit_code": None,
                "output_tail": [], "events": [], "last_join": None}

    def signal(self, _sig) -> None:
        return None


class _RecordingChild(_StubChild):
    """记录 Child(name, argv, cwd, extra_env) 的构造入参（supervisor 就是这么建的）。"""

    def __init__(self, name, argv=None, cwd=None, extra_env=None, **kwargs) -> None:  # noqa: ARG002
        super().__init__(name=name, argv=argv)
        self._env = dict(extra_env or {})


@pytest.fixture()
def _restore_environ():
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
    s.bridge = _StubChild(name="rtc_bridge")
    s.sidecar = _StubChild(name="sidecar")
    s.audio = None
    s._audio_plan = None
    s._audio_status = {"ok": False, "state": "not-started", "error": ""}
    s.audio_runtime_dir = "/tmp/jax-audio"
    s.audio_ready_timeout_s = 0.01
    return s


def _start_audio_with(monkeypatch, tmp_path: Path, report) -> sup.BridgeSupervisor:
    s = _bare_supervisor()
    plan = audio_env.build_plan(tmp_path, which=lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(audio_env, "build_plan", lambda *a, **k: plan)
    monkeypatch.setattr(audio_env, "wait_for_socket", lambda *a, **k: True)
    monkeypatch.setattr(sup, "Child", _RecordingChild)
    monkeypatch.setattr(sup.audio_env, "inspect_devices", lambda *a, **k: report)
    s._start_audio()
    return s


def test_runtime_inventory_is_measured_not_planned(monkeypatch, tmp_path: Path,
                                                   _restore_environ) -> None:
    """计划里有 sink、实测里没有：两个键必须各说各话，否则一条 GET 判不了死。"""
    report = {
        "probed": True, "sinks": [], "sources": [], "clients": [],
        "sink_present": False, "source_present": False,
        "default_sink": None, "default_source": None,
    }
    s = _start_audio_with(monkeypatch, tmp_path, report)

    assert s._audio_status["ok"] is True, "清点发现没设备也不得改变启动/退出语义（观测 fail-open）"
    assert s._audio_status["devices"]["playout_ok"] is False
    assert s._audio_status["devices"]["sink_count"] == 0
    # 计划值仍在：它是"我们打算造什么"，不是证据。
    assert s._audio_status["sink"] == audio_env.SINK_NAME


def test_runtime_inventory_failure_is_unknown_and_still_starts(monkeypatch, tmp_path: Path,
                                                               _restore_environ) -> None:
    def _boom(*_a, **_k):
        raise PermissionError("pactl 被 SELinux 拦了")

    s = _bare_supervisor()
    plan = audio_env.build_plan(tmp_path, which=lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(audio_env, "build_plan", lambda *a, **k: plan)
    monkeypatch.setattr(audio_env, "wait_for_socket", lambda *a, **k: True)
    monkeypatch.setattr(sup, "Child", _RecordingChild)
    monkeypatch.setattr(sup.audio_env, "inspect_devices", _boom)

    s._start_audio()  # 不得抛

    devices = s._audio_status["devices"]
    assert devices["probed"] is False
    assert devices["playout_ok"] is None, "清点异常 = 不知道，不是'没有设备'"
    assert "PermissionError" in devices["error"]


def test_empty_sink_is_reported_as_error_not_swallowed(monkeypatch, tmp_path: Path,
                                                      caplog, _restore_environ) -> None:
    """清点成功却一个 sink 都没有时必须是 ERROR 一行 —— 那正是本次事故的直接条件。"""
    report = {
        "probed": True, "sinks": [], "sources": [], "clients": [],
        "sink_present": False, "source_present": False,
        "default_sink": None, "default_source": None,
    }
    with caplog.at_level("INFO", logger="jax-voice-bridge"):
        _start_audio_with(monkeypatch, tmp_path, report)

    assert any(
        record.levelname == "ERROR" and "playout_ok=False" in record.getMessage()
        for record in caplog.records
    ), caplog.text


def test_status_carries_the_measured_devices(monkeypatch, tmp_path: Path,
                                             _restore_environ) -> None:
    report = {
        "probed": True, "sinks": ["0\tjax_null\t…"], "sources": ["1\tjax_null.mic\t…"],
        "clients": ["liteav"], "sink_present": True, "source_present": True,
        "default_sink": "jax_null", "default_source": "jax_null.monitor",
    }
    s = _start_audio_with(monkeypatch, tmp_path, report)
    s.bridge_health_url = "http://127.0.0.1:19093/health"
    s.sign_url = ""
    s.device_id = "jax-cloud-bridge"
    monkeypatch.setattr(s, "_probe_bridge_health", lambda: "ok")

    payload = s.status()

    devices = payload["audio"]["devices"]
    assert devices["playout_ok"] is True
    assert devices["client_names"] == ["liteav"], "谁是 pulseaudio 的客户端必须能从 /status 读到"
    assert devices["default_sink"] == "jax_null"


# --- 5. 事件环：`[ADEV]` 必须活得下来，且不得被 [VOL] 借道 -------------------


def test_adev_marker_is_in_the_event_ring() -> None:
    module = importlib.util.spec_from_file_location(
        "jax_voice_bridge_supervisor_adev", CLOUDBRIDGE / "supervisor.py"
    )
    assert module is not None and module.loader is not None
    loaded = importlib.util.module_from_spec(module)
    sys.modules[module.name] = loaded
    module.loader.exec_module(loaded)

    line = ("[2026-09-16T14:22:32.344Z] [ADEV] 进房后设备清点 speakers=0(空) mics=1(JaxNullMic) "
            "无播放设备 ⇒ TRTC 播放管线不会启动，远端帧不会回调（onPlayAudioFrame），上行恒为 0")
    assert loaded.is_event_line(line) is True, "设备清点行必须留在事件环里（300 行 tail 会被 [VOL] 挤空）"
    # 噪声优先的既有顺序不得被新标记破坏。
    assert loaded.is_event_line("[2026-09-16T14:22:32.344Z] [VOL] [:0] total=0 [ADEV]") is False
