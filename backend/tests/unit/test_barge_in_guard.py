"""Barge-in 误杀回复的防护契约（RED 先行）

真机实锤（logs/rtc_bridge_app.log，2026-09-07）：
- 15:00:41.931 first_audio_delta（AI 回复首帧）→ 15:00:41.952 barge_in open
  —— 仅 21ms！回复刚起步就被本地能量触发掐死。用户体感「没讲完、像卡了」。
- 14:23 会话：一条回复 7 秒内被 4 次 barge-in flush。
- 同期 3 个 ttfb=0-16ms 的瞬时 response（云端把回声 commit 成了输入）。

根因：session.on_up_audio 的本地 barge-in 触发（AI 播报中单帧 RMS>800 即开窗）
① 无播放起始宽限期——AEC 残差回声瞬态恰好集中在起播瞬间；
② 单帧即触发——任何一帧尖峰（咔哒声/截断噪声）都能杀掉整条回复。

修复契约：
1. 宽限期：AI 起播后 barge_grace_s 秒内不得开打断窗
2. 持续语音：需连续 barge_sustain_frames 帧高能量才开窗（帧间隔即上行 20ms）
3. 真实打断不受影响：宽限期后持续高能量 → 正常开窗 flush
4. 能量序列中断（安静帧）→ 连续计数归零（防偶发尖峰累积误触发）
"""
from __future__ import annotations

import asyncio
import time

import pytest

from rtc_bridge.session import PeerVoiceSession


class StubApm:
    """可显式置 closed/dead 的 ApmBridge 替身（与 test_session_voice_intent 同款）"""

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


LOUD = b"\x00\x40" * 320   # 小端 0x4000=16384 → RMS≈16384，远超 800 阈值
QUIET = b"\x00\x01" * 320  # 小端 0x0100=256 → RMS≈256，低于 800 阈值


def _make_session(**kw) -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        **kw,
    )


async def _start_ai_speech(s: PeerVoiceSession) -> None:
    await s.start()
    await s.on_peer_enter("user-1")
    await s._on_audio_out(QUIET)
    assert s._down_speaking is True


@pytest.mark.asyncio
async def test_barge_in_ignored_within_grace_period(stub_apm):
    """契约 1：起播后 barge_grace_s 内，连续高能量帧也不得开打断窗

    真机对应：15:00:41.931 first_audio_delta → 41.952 barge_in（21ms）。
    """
    s = _make_session(barge_grace_s=0.5)
    await _start_ai_speech(s)

    for _ in range(10):  # 10 帧 = 200ms，全部在 0.5s 宽限期内
        await s.on_up_audio(LOUD)

    assert s._barge_in is False, (
        "起播 200ms 内的连续高能量（回声瞬态集中区）不得开打断窗；"
        "旧行为 21ms 就把回复掐死，正是「没讲完」的直接原因"
    )

    # 宽限期过后（推进起始时刻模拟时间流逝），持续语音应正常开窗
    s._down_speaking_since = time.time() - 1.0
    for _ in range(3):
        await s.on_up_audio(LOUD)
    assert s._barge_in is True, "宽限期后的持续高能量必须正常触发打断（不牺牲真实打断）"


@pytest.mark.asyncio
async def test_barge_in_requires_sustained_speech(stub_apm):
    """契约 2：单帧/双帧尖峰不触发，连续 N 帧才触发"""
    s = _make_session(barge_grace_s=0.0, barge_sustain_frames=3)
    await _start_ai_speech(s)

    await s.on_up_audio(LOUD)
    await s.on_up_audio(LOUD)
    assert s._barge_in is False, "1-2 帧尖峰不应开窗（旧行为单帧即触发）"

    await s.on_up_audio(LOUD)
    assert s._barge_in is True, "连续 3 帧高能量应开窗"


@pytest.mark.asyncio
async def test_quiet_frame_resets_sustain_counter(stub_apm):
    """契约 4：高能量帧被安静帧打断 → 连续计数归零"""
    s = _make_session(barge_grace_s=0.0, barge_sustain_frames=3)
    await _start_ai_speech(s)

    await s.on_up_audio(LOUD)
    await s.on_up_audio(LOUD)
    await s.on_up_audio(QUIET)   # 中断序列
    await s.on_up_audio(LOUD)
    await s.on_up_audio(LOUD)

    assert s._barge_in is False, "安静帧打断序列后不足 3 连帧，不得开窗"

    await s.on_up_audio(LOUD)    # 第 3 个连续帧
    assert s._barge_in is True
