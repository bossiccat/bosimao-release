"""播放期上行门控（RED 先行）

真机实锤（logs/rtc_bridge_app.log 2026-09-07 15:00:43-46）：AI 播报期间，
扬声器回声随上行照喂云端 → Qwen smart_turn 把回声 commit 成用户输入 →
产生 3 个 ttfb=0-16ms 的瞬时垃圾 response，逐个再被 barge-in flush，
用户体感「卡、没讲完」。barge-in 宽限期（commit 4d6bcf1）已保护下行，
但上行回声仍在喂——本契约堵住源头。

修复契约：
1. AI 播报中（_down_speaking 且非 barge-in）：上行帧不喂云端，计数可见
2. barge-in 开窗后：上行恢复喂云端（用户打断的语音必须可达云端，否则
   云端不知道用户说了什么，无法生成新回复）
3. 非播报期：上行照常喂（不回归）
"""
from __future__ import annotations

import asyncio
import time

import pytest

from rtc_bridge.session import PeerVoiceSession


class StubApm:
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


async def _sent(msg: dict) -> None:
    pass


QUIET = b"\x00\x01" * 320  # 小端 0x0100=256 RMS，低于 800 阈值
LOUD = b"\x00\x40" * 320   # 0x4000=16384 RMS


def _make_session(**kw) -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        **kw,
    )


@pytest.mark.asyncio
async def test_playback_gates_uplink_to_cloud(stub_apm):
    """契约 1：AI 播报中上行不喂云端（回声不进 smart_turn），计数可见"""
    s = _make_session(barge_grace_s=0.5)
    await s.start()
    await s.on_peer_enter("user-1")

    # AI 开始播报
    await s._on_audio_out(QUIET)
    assert s._down_speaking is True

    # 播报期间的上行帧（回声/用户语音）不得进云端
    for _ in range(5):
        await s.on_up_audio(QUIET)
    await asyncio.sleep(0.05)

    assert s.apm.fed == [], (
        "播报期间上行帧不得喂云端——回声被 smart_turn commit 正是垃圾 response 的源头"
    )
    assert s.stats.get("up_gated_playback", 0) == 5, "门控丢弃必须有计数（可观测）"

    # 播报结束（强制推进静默判定）→ 上行恢复喂云端
    s._last_down_ts = time.time() - 1.0
    await s._check_down_speaking_over()
    assert s._down_speaking is False
    await s.on_up_audio(QUIET)
    await asyncio.sleep(0.05)
    assert len(s.apm.fed) == 1, "播报结束后上行必须恢复正常喂云端（不回归）"


@pytest.mark.asyncio
async def test_barge_in_reopens_uplink(stub_apm):
    """契约 2：barge-in 开窗后上行恢复喂云端（打断语音必须可达云端）"""
    s = _make_session(barge_grace_s=0.0, barge_sustain_frames=3)
    await s.start()
    await s.on_peer_enter("user-1")

    await s._on_audio_out(QUIET)
    assert s._down_speaking is True

    # 播报初期被门控
    await s.on_up_audio(QUIET)
    await asyncio.sleep(0.02)
    assert s.stats.get("up_gated_playback", 0) == 1

    # 用户真实打断（持续 3 帧高能量）→ barge-in 开窗
    s._down_speaking_since -= 1.0  # 越过宽限期
    for _ in range(3):
        await s.on_up_audio(LOUD)
    assert s._barge_in is True

    # 开窗后上行必须恢复喂云端（用户说了什么，云端得知道）
    await s.on_up_audio(QUIET)
    await asyncio.sleep(0.05)
    assert len(s.apm.fed) >= 1, (
        "barge-in 开窗后上行不得继续被门控——否则云端收不到打断内容，无法生成新回复"
    )
