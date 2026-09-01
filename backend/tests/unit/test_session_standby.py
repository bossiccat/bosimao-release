"""GPT-Live standby/wake behavior tests for PeerVoiceSession."""
from __future__ import annotations

import pytest

from rtc_bridge.session import PeerVoiceSession


class StubApm:
    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, **kwargs) -> None:
        self.on_audio_out = on_audio_out
        self.on_text = on_text
        self.closed = False
        self.dead = False
        StubApm.instances.append(self)

    async def feed_pcm(self, pcm: bytes) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


async def _send(_msg: dict) -> None:
    return None


def make_session(monkeypatch, intents: list[str] | None = None, sent: list[dict] | None = None) -> PeerVoiceSession:
    monkeypatch.setattr("rtc_bridge.session.ApmBridge", StubApm)
    async def send(msg: dict) -> None:
        if sent is not None:
            sent.append(msg)
    async def route(text: str) -> None:
        if intents is not None:
            intents.append(text)
    return PeerVoiceSession(
        device_id="dev", room_id="room", send_msg=send,
        apm_api_url="ws://fake", apm_system_prompt="prompt",
        on_voice_intent=route,
    )


def make_session_legacy(monkeypatch, intents: list[str] | None = None) -> PeerVoiceSession:
    monkeypatch.setattr("rtc_bridge.session.ApmBridge", StubApm)
    async def route(text: str) -> None:
        if intents is not None:
            intents.append(text)
    return PeerVoiceSession(
        device_id="dev", room_id="room", send_msg=_send,
        apm_api_url="ws://fake", apm_system_prompt="prompt",
        on_voice_intent=route,
    )


@pytest.mark.asyncio
async def test_standby_marker_activates_after_utterance(monkeypatch):
    s = make_session(monkeypatch)
    await s.start()
    await s.on_peer_enter("user")
    await s._on_audio_out(b"\x00\x10" * 320)
    await s._on_text("好的，我退下了[STANDBY]")
    assert s._standby is False
    assert s._standby_pending is True
    s._last_down_ts = 0
    await s._check_down_speaking_over()
    assert s._standby is True
    await s.close()


@pytest.mark.asyncio
async def test_standby_drops_downlink_audio(monkeypatch):
    s = make_session(monkeypatch)
    await s.start()
    await s.on_peer_enter("user")
    s._standby = True
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["standby_drops"] == 1
    assert s.shaper.metrics()["queue_depth"] == 0
    await s.close()


@pytest.mark.asyncio
async def test_active_marker_exits_standby(monkeypatch):
    s = make_session(monkeypatch)
    await s.start()
    await s.on_peer_enter("user")
    s._standby = True
    await s._on_text("我在[ACTIVE]")
    assert s._standby is False
    assert s._standby_pending is False
    await s.close()
