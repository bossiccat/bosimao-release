"""打断延迟的**桥侧权威口径**（2026-09-13 RED 先行）

为什么要换口径
--------------
手机侧 `onPlayAudioFrame` 拿不到 reply_id，只能靠「能量静音」猜旧回复何时结束，
两个口径都被实测证明不可靠：
  · 旧口径「插话 → 最后一帧含能量的帧」会把模型对插话的**新回复**也算成「还在说」
    ⇒ 系统性高估（历史值 1154/1664/1788）；
  · 新口径「插话后第一个 ≥200ms 静音间隙」会被**句间停顿**误判为结束，且实测给出 null。

桥侧天然知道 reply 身份（`DownFrame.reply_id`）与插话时刻。本测试守住：
1. 打断被检测到的那一刻记 barge_t0（**本进程 time.monotonic()**）与旧 reply_id；
2. 产出可分段判读的权威日志：
   `[lat] barge_in stop old_reply=<id> frames=<n> detect_ms=<d> emit_stop_ms=<e>
    last_sent_ms=<l> residual_frames=<r> reason=<...>`
3. **永不 null**，且 `emit_stop_ms` 必须量"我们停止送出旧回复"的时刻。

⚠️ 2026-09-13 修正：初版把 finish 线定在「旧 reply **最后一帧被送出**」，量出来恒为 0 ——
因为打断时 `shaper.reset()` 把**还排在队列里**的残余帧（实测 158 帧≈3.16s）整批丢掉，
"最后一帧送出"发生在打断**之前**。0ms 在物理上不可能，所以那是量错事件，不是延迟为 0。
现在拆成 `detect_ms`（我们的决策成本）/ `emit_stop_ms`（我们停止送出）/ `last_sent_ms`（送入节拍器，
⚠️≠ 用户听到的时刻）。断言里显式禁止裸 `stop_ms=` 字段复活。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import pytest

from rtc_bridge.session import PeerVoiceSession

# 其它测试模块会 `logging.disable(logging.WARNING)`（全局副作用）——本文件需要 INFO 级日志
logging.disable(logging.NOTSET)

FRAME = b"\x00\x40" * 320   # 640B，rms≈16384；用作能正常送出的下行帧

_STOP_RE = re.compile(
    r"barge_in stop old_reply=(\S+) frames=(\d+) detect_ms=(-?\d+) "
    r"emit_stop_ms=(-?\d+) last_sent_ms=(-?\d+) residual_frames=(\d+) reason=(\S+)"
)
# 裸的 stop_ms= 是**被废弃的错口径**（把它当延迟会得到 0ms 的假象），不得复活。
_STALE_FIELD_RE = re.compile(r"(?<!_)\bstop_ms=")


class StubApm:
    """ApmBridge 替身（惰性、不连网）。与 test_barge_in_guard 同款边界替身。"""

    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, on_state=None,
                 on_error=None, api_url="", system_prompt="", token="") -> None:
        self.on_audio_out = on_audio_out
        self.on_text = on_text
        self.on_state = on_state
        self.on_error = on_error
        self.fed: list[bytes] = []
        self.closed = False
        self.dead = False
        StubApm.instances.append(self)

    async def feed_pcm(self, pcm: bytes) -> None:
        if self.closed or self.dead:
            return
        self.fed.append(pcm)

    async def close(self) -> None:
        self.closed = True

    async def start(self) -> None:
        pass


@pytest.fixture
def stub_apm(monkeypatch):
    StubApm.instances = []
    monkeypatch.setattr("rtc_bridge.session.ApmBridge", StubApm)
    return StubApm.instances


def _make_session(sent: list[dict], **kw) -> PeerVoiceSession:
    async def send_msg(msg: dict) -> None:
        sent.append(msg)

    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=send_msg,
        apm_api_url="ws://fake", apm_system_prompt="p",
        barge_grace_s=0.0, barge_sustain_frames=3, **kw,
    )


def _stop_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "barge_in stop" in r.getMessage()]


@pytest.mark.asyncio
async def test_local_barge_emits_segmented_latency_non_null(stub_apm, caplog):
    """本地能量打断 → 出数（frames>0，detect/emit_stop 均 >=0，且不含 None/n/a）"""
    caplog.set_level(logging.INFO, logger="rtc_bridge.session")
    sent: list[dict] = []
    s = _make_session(sent)
    await s.start()
    await s.on_peer_enter("user-1")
    try:
        # 推一大段下行（30 帧），让整形器有积压、其中若干帧先真的送出
        await s._on_audio_out(FRAME * 30)
        await asyncio.sleep(0.30)
        assert s._down_speaking is True
        assert s.shaper.queued_frames >= 0

        # 越过宽限期后连续 3 帧高能量 → 本地能量打断开窗
        s._down_speaking_since = time.time() - 1.0
        s._barge_in = False
        for _ in range(3):
            await s.on_up_audio(FRAME)
        assert s._barge_in is True, "持续高能量应触发本地打断"

        # down_silent 结算在 _check_down_speaking_over 里（下行静默 >0.6s）
        await asyncio.sleep(1.2)
    finally:
        await s.close()

    lines = _stop_lines(caplog)
    assert lines, "打断后必须产出 [lat] barge_in stop 日志（权威口径）"
    m = _STOP_RE.search(lines[0])
    assert m, f"日志格式必须可直接判读，实测 {lines[0]!r}"
    frames = int(m.group(2))
    detect_ms, emit_stop_ms = int(m.group(3)), int(m.group(4))
    assert frames > 0, "打断前应已有旧回复帧送出"
    assert detect_ms >= 0, "detect_ms（我们的决策成本）必须 >= 0"
    assert emit_stop_ms >= 0, "emit_stop_ms（我们停止送出旧回复的时刻）必须 >= 0"
    assert "None" not in lines[0] and "n/a" not in lines[0], "权威口径永不 null"
    assert not _STALE_FIELD_RE.search(lines[0]), (
        "废弃的裸 stop_ms= 口径不得复活 —— 它把 finish 线定在被打断丢弃的残余帧之前，"
        "恒得 0ms，是量错事件而不是延迟为 0"
    )


@pytest.mark.asyncio
async def test_barge_stop_settles_on_close_with_reason(stub_apm, caplog):
    """会话在结算前结束 → 仍出数，并带 reason=close（不静默、不 null）"""
    caplog.set_level(logging.INFO, logger="rtc_bridge.session")
    sent: list[dict] = []
    s = _make_session(sent)
    await s.start()
    await s.on_peer_enter("user-1")
    await s._on_audio_out(FRAME * 30)
    await asyncio.sleep(0.25)

    s._down_speaking_since = time.time() - 1.0
    s._barge_in = False
    for _ in range(3):
        await s.on_up_audio(FRAME)
    assert s._barge_in is True
    assert s._barge_lat_pending is not None, "打断必须已 arm 一次测量"

    await s.close()  # 结算前结束会话

    lines = _stop_lines(caplog)
    assert lines and "reason=close" in lines[-1], \
        f"会话结束应带 reason=close 结算，实测 {lines!r}"
    assert _STOP_RE.search(lines[-1]), "必须出数（各分段均为 int）"


@pytest.mark.asyncio
async def test_barge_stop_without_old_reply_frames_still_logs(stub_apm, caplog):
    """旧回复从未送出过帧（打断发生在首帧前）→ frames=0 stop_ms=0，不抛异常不静默"""
    caplog.set_level(logging.INFO, logger="rtc_bridge.session")
    sent: list[dict] = []
    s = _make_session(sent)
    await s.start()
    await s.on_peer_enter("user-1")
    try:
        # 人为置「正在播报」但从未铸造 reply / 送出任何帧
        s._down_speaking = True
        s._down_speaking_since = time.time() - 1.0
        s._barge_in = False
        for _ in range(3):
            await s.on_up_audio(FRAME)
        assert s._barge_in is True
        assert s._barge_lat_pending is not None
        s._settle_barge_latency("unit_test")
    finally:
        await s.close()

    lines = _stop_lines(caplog)
    assert lines, "无已送出帧也必须出数（带原因的日志）"
    m = _STOP_RE.search(lines[0])
    assert m, f"日志必须可直接判读，实测 {lines[0]!r}"
    assert m.group(2) == "0", f"无已送出帧应记 frames=0，实测 {lines[0]!r}"
    assert int(m.group(4)) >= 0, "即使无已送出帧，emit_stop_ms 仍须有值（>=0）"
    assert int(m.group(5)) == -1, "无已送出帧时 last_sent_ms 记 -1（明确不可得，不用 0 冒充）"
