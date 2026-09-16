"""jax-voice-bridge：`/status` 必须自己带出「关键事件」而不只是「最近 N 行」。

背景（2026-09-16 的真实代价）
------------------------------
真实 sidecar 打开 `--enable-logging=stderr` 后，TRTC 音量回调**每 500ms** 打一条
`[VOL] [:0] total=0`（实测见 sidecar/logs/sidecar-sidecar.log）。`output_tail` 是无差别
的「最近 N 行」环（sidecar 300 行），于是它只够盖约 2 分钟：

- `[ROOM] 进房成功（elapsed=348ms）` / `[ROOM] 进房失败 errCode=-1001`
- `[SIG] 意图轮询`、`[PEER] 远端加入`、`[BOOT] role=sidecar`

这些「一行定生死」的行全被挤出窗口；而本 CloudRun 的 CLS 主题只支持
`queryString="*"` 全量检索、关键词过滤返回 null（已实测），**外部没有任何手段**能把
那一刻的行捞回来。⇒ 判据必须由 `/status` 自己给出。

本文件断言的是**行为**（喂真实格式的行给 `Child`，读它报出来的字段），不是扫源码文本：
`[VOL]` 噪声行绝不进事件环、进房行必须在、`last_join` 取最后一次、环容量上限生效、
`output_tail` 的既有语义（无差别 + 原容量）不被改动。
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "jax_voice_bridge_supervisor_event_ring", CLOUDBRIDGE / "supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 逐行取自真实形态，不是凭空合成 -----------------------------------------
# 出处：sidecar/logs/sidecar-sidecar.log（`[ISO] [scope] msg`，sidecar/logger.js:19）
#       + sidecar/rtc.js:207 / :221 的进房两行（成功带 elapsed、失败带 errCode）。
VOL_LINE = "[2026-09-16T03:07:50.277Z] [VOL] [:0] total=0"
VOL_LINE_LATER = "[2026-09-16T03:09:51.785Z] [VOL] [:0] total=0"
JOIN_SUCCESS_LINE = "[2026-09-16T03:10:00.000Z] [ROOM] 进房成功（elapsed=348ms）"
JOIN_FAILURE_LINE = "[2026-09-16T03:11:00.000Z] [ROOM] 进房失败 errCode=-1001"

# 事件环必须留住的其它真实形态（每类都是 2026-09-16 排查里真正在等的一行）。
OTHER_EVENT_LINES = (
    "[2026-09-16T03:07:49.471Z] [BOOT] role=sidecar",
    "[2026-09-16T03:07:49.775Z] [SIG] 意图轮询已启动（每 2s），等待手机唤醒...",
    "[2026-09-16T03:07:50.473Z] [ERR] 意图轮询失败: Failed to fetch",
    "[2026-09-16T03:12:00.000Z] [PEER] 远端加入 jax-sim-phone",
    "[2026-09-16T03:12:01.000Z] [PCM] up frames=156",
    "[2026-09-16T03:12:02.000Z] [UPRMS] rms=0.031",
    "[2026-09-16T03:12:03.000Z] [STAT] elapsed=1s",
    "[2026-09-16T03:12:04.000Z] [WARN] ws retry",
    "[2026-09-16T03:12:05.000Z] FATAL hello-redeem failed",
    "[2026-09-16T03:12:06.000Z] Error: Cannot find module 'trtc-electron-sdk'",
    "[2026-09-16T03:12:07.000Z] rtc session created",
    "[2026-09-16T03:12:08.000Z] ws connected",
)

# 渲染进程里与排查无关的普通行：既不进事件环，也不必被特殊处理。
NOISE_ONLY_LINES = (
    "[2026-09-16T03:12:09.000Z] [PQ] pacer 已启动（frameMs=20）",
    "[2026-09-16T03:12:10.000Z] [console:info] some renderer chatter",
)


def _child(module, name: str = "sidecar"):
    """未启动的 Child：只喂行、不拉进程（stdout 泵之外的逻辑都能被行为断言）。"""
    return module.Child(name, [], Path("."), {})


def _feed(child, lines):
    for line in lines:
        child._ingest(line)


# --- 1. [VOL] 噪声绝不进事件环 ----------------------------------------------


def test_volume_frames_never_enter_the_event_ring():
    """每 500ms 一条的音量行是今天把进房事件挤出窗口的唯一原因，必须被挡在环外。"""
    module = _load_supervisor()
    child = _child(module)
    _feed(child, [VOL_LINE, VOL_LINE_LATER, VOL_LINE, JOIN_SUCCESS_LINE, VOL_LINE_LATER])

    events = child.describe()["events"]
    assert events, "事件环不得为空——否则等于没有观测入口"
    assert not [line for line in events if "[VOL]" in line], (
        f"`[VOL]` 高频噪声行漏进了事件环: {events}"
    )
    assert JOIN_SUCCESS_LINE in events


def test_volume_line_that_also_carries_a_marker_is_still_excluded():
    """噪声判定优先于标记匹配：一旦漏进来就会重演「300 行只够 2 分钟」。

    构造的是对抗性输入（音量行里带上 errCode 字样），用来锁定过滤顺序，
    而不是依赖"真实音量行恰好不含标记词"这种巧合。
    """
    module = _load_supervisor()
    child = _child(module)
    adversarial = "[2026-09-16T03:13:00.000Z] [VOL] [:0] total=0 errCode=0"
    _feed(child, [adversarial, JOIN_FAILURE_LINE])

    events = child.describe()["events"]
    assert adversarial not in events
    assert JOIN_FAILURE_LINE in events


# --- 2. 关键事件必须在环里 ---------------------------------------------------


@pytest.mark.parametrize("line", OTHER_EVENT_LINES)
def test_every_marker_line_is_kept(line):
    module = _load_supervisor()
    child = _child(module)
    _feed(child, [VOL_LINE, line])

    assert line in child.describe()["events"], f"关键事件行被丢弃: {line}"


def test_plain_renderer_lines_are_not_promoted_to_events():
    module = _load_supervisor()
    child = _child(module)
    _feed(child, NOISE_ONLY_LINES)

    assert child.describe()["events"] == []


# --- 3. last_join：最后一次进房结果一条 GET 就能读到 --------------------------


def test_last_join_reports_failure_when_it_overrides_an_earlier_success():
    """失败那次必须盖掉成功那次：否则「上一轮成功、这一轮失败」会被读成一切正常。"""
    module = _load_supervisor()
    child = _child(module)
    _feed(child, [JOIN_SUCCESS_LINE, VOL_LINE, JOIN_FAILURE_LINE, VOL_LINE_LATER])

    last_join = child.describe()["last_join"]
    assert last_join is not None
    assert last_join["outcome"] == "failure"
    assert last_join["line"] == JOIN_FAILURE_LINE, "原文必须原样保留"
    assert last_join["err_code"] == -1001
    assert last_join["elapsed_ms"] is None, "失败行没有 elapsed，不得凭空造一个"


def test_last_join_reports_the_latest_success_with_elapsed():
    module = _load_supervisor()
    child = _child(module)
    _feed(child, [JOIN_FAILURE_LINE, JOIN_SUCCESS_LINE])

    last_join = child.describe()["last_join"]
    assert last_join["outcome"] == "success"
    assert last_join["line"] == JOIN_SUCCESS_LINE
    assert last_join["elapsed_ms"] == 348
    assert last_join["err_code"] is None


def test_last_join_falls_back_to_raw_text_when_fields_are_unparseable():
    """真实 phone.js:251 的失败行只有裸结果码、没有 `errCode=` 标签。

    这种行也必须能被读到（原文在、字段为 None），绝不因为解析失败就整条丢掉。
    """
    module = _load_supervisor()
    child = _child(module)
    phone_failure = "[2026-09-16T03:14:00.000Z] [PHONE] 进房失败 -1002"
    _feed(child, [phone_failure])

    last_join = child.describe()["last_join"]
    assert last_join["outcome"] == "failure"
    assert last_join["line"] == phone_failure
    assert last_join["err_code"] is None and last_join["elapsed_ms"] is None


def test_last_join_is_null_when_no_join_was_ever_attempted():
    """`null` 是有意义的信息：区分「从未尝试进房」与「尝试了但失败」。"""
    module = _load_supervisor()
    child = _child(module)
    _feed(child, [VOL_LINE, "[2026-09-16T03:07:49.471Z] [BOOT] role=sidecar"])

    assert child.describe()["last_join"] is None


# --- 4. 环容量上限 ----------------------------------------------------------


def test_event_ring_keeps_only_the_last_events_up_to_capacity():
    module = _load_supervisor()
    capacity = module.EVENT_TAIL_LINES
    assert capacity == 200, "容量是运维口径的一部分（约覆盖多久的关键事件）"

    child = _child(module)
    overflow = capacity + 37
    _feed(child, [f"[STAT] seq={i}" for i in range(overflow)])

    events = child.describe()["events"]
    assert len(events) == capacity
    assert events[-1] == f"[STAT] seq={overflow - 1}"
    assert events[0] == f"[STAT] seq={overflow - capacity}"
    assert f"[STAT] seq={overflow - capacity - 1}" not in events


# --- 5. output_tail 的既有语义与容量不变（有人在用）--------------------------


def test_output_tail_still_carries_every_line_and_its_own_capacity():
    """事件环是**并列新增**，不得把 output_tail 变成"也过滤过的"或缩小它。"""
    module = _load_supervisor()
    child = _child(module)
    assert child.tail_lines == module.Child.TAIL_LINES == 120

    _feed(child, [VOL_LINE, VOL_LINE_LATER] + list(NOISE_ONLY_LINES) + [JOIN_FAILURE_LINE])
    described = child.describe()

    assert described["output_tail"] == [
        VOL_LINE, VOL_LINE_LATER, *NOISE_ONLY_LINES, JOIN_FAILURE_LINE,
    ], "output_tail 必须保持无差别全量（含音量行）"
    assert described["events"] == [JOIN_FAILURE_LINE]


def test_output_tail_capacity_is_applied_independently_of_events():
    module = _load_supervisor()
    child = _child(module)
    for i in range(child.tail_lines + 25):
        child._ingest(f"[STAT] line={i}")

    described = child.describe()
    assert len(described["output_tail"]) == child.tail_lines
    assert len(described["events"]) == child.tail_lines + 25, (
        "事件条数不得被 output_tail 的容量裁剪"
    )


# --- 6. /status 必须一次 GET 就带出来 ---------------------------------------


def test_status_exposes_events_and_last_join_under_the_sidecar():
    """验收口径：一次 GET /api/v1/voice/bridge/status 就能判定"进房了没有"。"""
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.sim_enabled = False
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = "https://example.invalid"
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.bridge = _child(module, "rtc_bridge")
    sup.sidecar = _child(module, "sidecar")
    _feed(sup.bridge, ["[STAT] ws client connected"])
    _feed(sup.sidecar, [VOL_LINE, JOIN_SUCCESS_LINE, VOL_LINE_LATER, JOIN_FAILURE_LINE])
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")

    payload = sup.status()

    assert JOIN_FAILURE_LINE in payload["sidecar"]["events"]
    assert not [line for line in payload["sidecar"]["events"] if "[VOL]" in line]
    assert payload["sidecar"]["last_join"]["outcome"] == "failure"
    assert payload["sidecar"]["last_join"]["err_code"] == -1001
    # 另一个子进程同样带出来（rtc_bridge 侧的事件环），字段不因角色而缺失。
    assert payload["rtc_bridge"]["events"] == ["[STAT] ws client connected"]
    assert payload["rtc_bridge"]["last_join"] is None


# --- 7. 接线：过滤逻辑必须真的挂在 stdout 泵上 -------------------------------


def test_stdout_pump_feeds_both_rings(tmp_path):
    """真实子进程的 stdout 必须经同一个入口进两个环。

    其余用例直接喂 `_ingest`，证明的是"过滤函数写对了"；只有这一条能证明它**接在
    真实的 stdout 泵上**——否则把 `_pump` 改回只写 output_tail 也不会有人发现。
    """
    module = _load_supervisor()
    emitted = [VOL_LINE, JOIN_FAILURE_LINE, *NOISE_ONLY_LINES]
    script = tmp_path / "emit_lines.py"
    script.write_text(
        "for line in " + repr(emitted) + ":\n    print(line, flush=True)\n",
        encoding="utf-8",
    )
    child = module.Child(
        "emitter",
        [sys.executable, str(script)],
        tmp_path,
        # 子进程把中文写到管道：显式固定 UTF-8，避免 Windows 默认代码页编不出来。
        {"PYTHONIOENCODING": "utf-8"},
    )
    child.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(child.describe()["output_tail"]) < len(emitted):
            time.sleep(0.05)
    finally:
        child.signal(15)

    described = child.describe()
    assert described["output_tail"] == emitted, "stdout 尾部必须无差别收全（含音量行）"
    assert described["events"] == [JOIN_FAILURE_LINE], (
        f"stdout 泵没有把过滤后的行接进事件环: {described['events']}"
    )
    assert described["last_join"]["err_code"] == -1001
