from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app.voice.qwen_realtime_bridge import (
    QwenRealtimeBridge,
    QWEN_COORDINATION_TOOLS,
    pcm24k_to_pcm16k,
)


class FakeWs:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "session.update":
            self.incoming.put_nowait(json.dumps({"type": "session.updated"}))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_qwen_session_update_registers_coordination_tools(monkeypatch):
    ws = FakeWs()

    async def connect(url, token, system_prompt, tools):
        del url, token, system_prompt
        await ws.send(json.dumps({"type": "session.update", "session": {"tools": tools}}))
        return ws, "qwen-session"

    monkeypatch.setattr("app.voice.qwen_realtime_bridge.connect_qwen", connect)
    bridge = QwenRealtimeBridge(on_audio_out=_noop)
    await bridge.start()
    update = next(x for x in ws.sent if x["type"] == "session.update")
    names = [x["function"]["name"] for x in update["session"]["tools"]]
    assert names == [x["function"]["name"] for x in QWEN_COORDINATION_TOOLS]
    await bridge.close()


@pytest.mark.asyncio
async def test_qwen_audio_event_decodes_pcm16_24k_to_16k(monkeypatch):
    out = []
    bridge = QwenRealtimeBridge(on_audio_out=out.append)
    pcm24 = (b"\x01\x00" * 24000)
    event = {"type": "response.audio.delta", "delta": base64.b64encode(pcm24).decode()}
    await bridge._handle_event(event)
    assert len(out) == 1
    assert len(out[0]) == 16000 * 2


def test_qwen_approval_tool_schema_does_not_accept_model_approval_id():
    approval = next(x for x in QWEN_COORDINATION_TOOLS if x["function"]["name"] == "approve_reply")
    props = approval["function"]["parameters"]["properties"]
    assert "approval_id" not in props
    assert "approval_id" not in approval["function"]["parameters"].get("required", [])


def test_pcm24k_to_pcm16k_preserves_pcm_format():
    assert len(pcm24k_to_pcm16k(b"\x00\x00" * 24000)) == 16000 * 2


async def _noop(_audio):
    return None
