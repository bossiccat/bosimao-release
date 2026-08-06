"""EndDetectFeeder 单测（停顿补静音：说话/静音/补静音/重置 四态，PC-INTEGRATION §3.4）

说明：说完判定依赖 wall-clock（低能量持续 >1.2s），测试用可控假时钟推进。
"""
from __future__ import annotations

import pytest

from app.voice.end_detect import EndDetectFeeder, pcm_rms


def _voice(n_samples: int = 1600) -> bytes:
    """能量 >400 的"说话"帧（40ms @16k）"""
    import array

    return array.array("h", [8000] * n_samples).tobytes()


def _silence(n_bytes: int = 3200) -> bytes:
    return b"\x00\x00" * (n_bytes // 2)


PAD = b"\x00\x00" * 16000 * 2  # 2s @16k 纯静音


@pytest.fixture
def clock(monkeypatch):
    """可控假时钟：推进 fake_time 触发停顿判定"""
    state = {"now": 1000.0}
    monkeypatch.setattr("app.voice.end_detect.time.time", lambda: state["now"])
    return state


def _feeder(fake_feed, clock_state=None):
    return EndDetectFeeder(feed=fake_feed, silence_s=1.2, pad_s=2.0)


@pytest.mark.asyncio
async def test_voice_frames_forwarded(clock):
    fed: list[bytes] = []

    async def fake_feed(pcm: bytes) -> None:
        fed.append(pcm)

    feeder = EndDetectFeeder(feed=fake_feed)
    v = _voice()
    await feeder.feed(v)
    assert fed == [v]  # 说话帧原样转发


@pytest.mark.asyncio
async def test_silence_padded_after_1_2s(clock):
    fed: list[bytes] = []
    padded: list[bytes] = []

    async def fake_feed(pcm: bytes) -> None:
        fed.append(pcm)
        if pcm == PAD:
            padded.append(pcm)

    feeder = EndDetectFeeder(feed=fake_feed, silence_s=1.2, pad_s=2.0)
    await feeder.feed(_voice())
    clock["now"] += 0.5  # 静音 0.5s（未到阈值）
    await feeder.feed(_silence())
    assert padded == []
    clock["now"] += 1.0  # 累计静音 1.5s > 1.2s → 补 2s 静音（一次性）
    await feeder.feed(_silence())
    assert padded == [PAD]
    # 后续静音不再补（一次性标记）
    clock["now"] += 1.0
    await feeder.feed(_silence())
    assert len(padded) == 1


@pytest.mark.asyncio
async def test_voice_after_silence_resets_pad(clock):
    padded: list[bytes] = []

    async def fake_feed(pcm: bytes) -> None:
        if pcm == PAD:
            padded.append(pcm)

    feeder = EndDetectFeeder(feed=fake_feed, silence_s=1.2, pad_s=2.0)
    await feeder.feed(_voice())
    clock["now"] += 1.5
    await feeder.feed(_silence())
    assert len(padded) == 1
    # 用户再开口 → 重置 → 后续停顿再次补静音
    await feeder.feed(_voice())
    clock["now"] += 1.5
    await feeder.feed(_silence())
    assert len(padded) == 2


def test_pcm_rms_voice_vs_silence():
    assert pcm_rms(_voice()) > 400.0
    assert pcm_rms(_silence()) == 0.0


@pytest.mark.asyncio
async def test_reset_clears_pad_state(clock):
    padded: list[bytes] = []

    async def fake_feed(pcm: bytes) -> None:
        if pcm == PAD:
            padded.append(pcm)

    feeder = EndDetectFeeder(feed=fake_feed, silence_s=1.2, pad_s=2.0)
    await feeder.feed(_voice())
    clock["now"] += 1.5
    await feeder.feed(_silence())
    assert len(padded) == 1
    # reset（远端重进）→ 清补静音标记，且 last_voice 重置（需再停顿才补）
    feeder.reset()
    clock["now"] += 1.5
    await feeder.feed(_silence())
    assert len(padded) == 2
