"""P0-2：EndDetectFeeder 补静音 pad 按引擎区分（qwen=400ms，apm=2s）

根因（internal-latency-budget §1.1 U6）：静音 >1.2s 时一次性向云端注入 2s
全零 pad，用户 pad 后开口时云端 VAD 须先消化这 2s 静音 → speech_started
推迟最多 ~2s（上行空窗嫌疑 #2）。qwen 引擎自带 smart_turn 说完判定
（21:06 日志证实能产生 committed），2s pad 冗余 → 缩到 400ms；apm 引擎
无云端说完判定，保持 2s。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.voice.end_detect import EndDetectFeeder
from rtc_bridge.session import PeerVoiceSession


class StubApm:
    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, on_state=None,
                 on_error=None, api_url="", system_prompt="", token="") -> None:
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


def _make_session(voice_engine: str = "apm") -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        voice_engine=voice_engine,
    )


def test_qwen_session_uses_short_pad(stub_apm):
    """qwen 引擎：pad 400ms（smart_turn 自带说完判定，2s pad 冗余）"""
    s = _make_session(voice_engine="qwen")
    assert s.feeder._pad_s == pytest.approx(0.4), (
        f"qwen 引擎 pad 应为 0.4s，实际 {s.feeder._pad_s}s"
    )


def test_apm_session_keeps_2s_pad(stub_apm):
    """apm 引擎（无云端说完判定）：保持 2s pad"""
    s = _make_session(voice_engine="apm")
    assert s.feeder._pad_s == pytest.approx(2.0)


def test_peer_reenter_rebuild_keeps_engine_pad(stub_apm):
    """重进房重建桥后 pad 配置不丢（仍按引擎）"""
    s = _make_session(voice_engine="qwen")
    s.feeder = None
    import asyncio
    asyncio.get_event_loop_policy()
    # 手动走 rebuild 路径
    s._apm_rebuilds += 1
    s._build_apm()
    s.feeder = EndDetectFeeder(feed=s.apm.feed_pcm,
                               sample_rate=16000, pad_s=s._pad_s)
    assert s.feeder._pad_s == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_short_pad_injects_400ms_of_silence():
    """行为级：qwen pad 触发时注入 400ms 静音（6400 样本 = 12800 字节），非 2s"""
    fed: list[bytes] = []
    feeder = EndDetectFeeder(feed=fed.append if False else _capture(fed),
                             pad_s=0.4)
    # 一帧"语音"
    loud = b"\x10\x00" * 320
    await feeder.feed(loud)
    # 把 last_voice_ts 拨回远古，下一帧静音触发 pad
    feeder._last_voice_ts = time.time() - 2.0
    await feeder.feed(b"\x00\x00" * 320)
    pads = [f for f in fed if len(f) != 640 and set(f) == {0}]
    assert pads, "静音超阈值应注入 pad"
    assert len(pads[0]) == int(16000 * 0.4) * 2, (
        f"pad 应为 400ms（12800 字节），实际 {len(pads[0])} 字节（2s=64000 字节）"
    )


def _capture(buf: list[bytes]):
    async def _f(pcm: bytes) -> None:
        buf.append(pcm)
    return _f
