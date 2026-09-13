"""丢帧归因：队列身份（up/down）与分方向指标（2026-09-13）

背景：实测出现 `queue_drops=58` 且日志有 2 条 `audio age-drop`，但**无法判断是上行
还是下行丢的** —— /metrics 的 queue_drops 是 up+down 之和，age-drop WARNING 又不带
队列身份。本测试守住归因能力，**不改任何丢弃行为/预算数值**。
"""
from __future__ import annotations

import asyncio
import logging

from rtc_bridge.bounded_audio_queue import BoundedAudioQueue
from rtc_bridge.health import HealthServer
from rtc_bridge.session import PeerVoiceSession

FRAME = 640


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_age_drop_warning_carries_queue_name(caplog):
    """age-drop WARNING 必须带队列身份，否则 58 的丢帧无法归因"""
    caplog.set_level(logging.WARNING, logger="rtc_bridge.bounded_audio_queue")
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=100, max_bytes=100 * FRAME,
                          max_frame_age_ms=1000, now_fn=clock, name="down")
    q.push(b"a" * FRAME)
    clock.advance(2.0)          # 超龄
    q.push(b"b" * FRAME)        # 触发 _drop_expired

    recs = [r.getMessage() for r in caplog.records if "age-drop" in r.getMessage()]
    assert recs, "应有 age-drop 日志"
    assert "down" in recs[0], f"age-drop 日志必须含队列名，实测 {recs[0]!r}"


def test_metrics_exposes_queue_name():
    q = BoundedAudioQueue(max_frames=10, max_bytes=10 * FRAME,
                          max_frame_age_ms=1000, name="up")
    assert q.metrics()["queue_name"] == "up"


def test_default_name_is_empty_backward_compatible():
    """不传 name 时行为不变（旧调用点不破）"""
    q = BoundedAudioQueue(max_frames=10, max_bytes=10 * FRAME, max_frame_age_ms=1000)
    assert q.name == ""
    assert q.metrics()["queue_name"] == ""


def test_session_and_health_split_drops_by_direction(monkeypatch):
    """queue_drops 保留为两者之和；新增 up/down 分方向字段并透出 /metrics"""
    class _StubApm:
        def __init__(self, *a, **kw) -> None:
            self.closed = False
            self.started = False

        async def feed_pcm(self, pcm: bytes) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("rtc_bridge.session.ApmBridge", _StubApm)

    async def _send(msg: dict) -> None:
        pass

    async def _scenario() -> dict:
        # 需要事件循环：DownlinkShaper.__init__ 用 asyncio.get_event_loop()
        s = PeerVoiceSession(device_id="d", room_id="r", send_msg=_send,
                             apm_api_url="ws://fake", apm_system_prompt="p")
        s._up_q.drops = 3
        s.shaper._q.drops = 5
        s._sync_queue_metrics()
        return {"up": s.stats["queue_drops_up"], "down": s.stats["queue_drops_down"],
                "sum": s.stats["queue_drops"]}

    got = asyncio.run(_scenario())
    assert got["up"] == 3
    assert got["down"] == 5
    assert got["sum"] == 8, "queue_drops 必须仍是两者之和（既有消费方）"

    # /metrics 透出（health 只做搬运，这里用真实会话证明字段真的出去了）
    async def _health() -> dict:
        s = PeerVoiceSession(device_id="d", room_id="r", send_msg=_send,
                             apm_api_url="ws://fake", apm_system_prompt="p")
        s._up_q.drops = 3
        s.shaper._q.drops = 5
        s._sync_queue_metrics()
        hs = HealthServer("127.0.0.1", 0, {"room_id": "r", "_session_ref": s})
        m = hs._metrics()
        await s.close()
        return m

    m = asyncio.run(_health())
    assert m["queue_drops_up"] == 3
    assert m["queue_drops_down"] == 5
    assert m["queue_drops"] == 8


def test_health_exposes_playback_gate_drops(monkeypatch):
    """播放期上行门控的丢弃必须**单独**透出。

    它是与队列**无关**的另一条丢弃通路：AI 播报中（且打断尚未确认）每帧弹出即丢，
    目的是防扬声器回声被云端 commit 成用户输入。不与 queue_drops_* 分开，
    就会把「队列丢帧」与「刻意的门控丢弃」混为一谈 —— 2026-09-13 实测同一轮里
    `queue_drops_up=22` 而 `up_gated_playback=107`，量级差约 5 倍，混算必然误判。
    """
    class _StubApm:
        def __init__(self, *a, **kw) -> None:
            pass

        async def feed_pcm(self, pcm: bytes) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr("rtc_bridge.session.ApmBridge", _StubApm)

    async def _send(msg: dict) -> None:
        pass

    async def _health(gated: int | None) -> dict:
        s = PeerVoiceSession(device_id="d", room_id="r", send_msg=_send,
                             apm_api_url="ws://fake", apm_system_prompt="p")
        if gated is not None:
            s.stats["up_gated_playback"] = gated
        hs = HealthServer("127.0.0.1", 0, {"room_id": "r", "_session_ref": s})
        m = hs._metrics()
        await s.close()
        return m

    assert asyncio.run(_health(7))["up_gated_playback"] == 7
    assert asyncio.run(_health(None))["up_gated_playback"] == 0, "缺省必须为 0，不能 KeyError"
