"""ApmBridge 单元测试：转码 + 上行分块 + 会话装配（不依赖真实 API，用本地 WS 服务模拟）"""
from __future__ import annotations

import asyncio
import base64
import json

import numpy as np
import pytest

from app.voice.apm_bridge import ApmBridge, f32_to_s16_16k
from app.voice.config import VoiceConfig
from app.voice.session import VoiceSession, run_session


# ---------- 转码 ----------
def test_f32_24k_to_s16_16k():
    # 1s 24k f32（440Hz 正弦）→ 16k s16，长度应为 16000 样本 * 2B
    t = np.arange(24000) / 24000.0
    wave24 = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32).tobytes()
    out = f32_to_s16_16k(wave24)
    assert len(out) == 16000 * 2
    s16 = np.frombuffer(out, dtype=np.int16)
    assert s16.dtype == np.int16
    assert np.max(np.abs(s16)) > 1000  # 有有效信号


def test_f32_24k_to_s16_16k_silence():
    sil = np.zeros(24000, dtype=np.float32).tobytes()
    out = f32_to_s16_16k(sil)
    assert len(out) == 32000
    assert all(b == 0 for b in out)


# ---------- 上行分块（1s 累积） ----------
class FakeWs:
    """模拟 API 服务端：记录收到的 input.append"""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.send_queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def recv(self):
        return await self.send_queue.get()

    async def send(self, data) -> None:
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        msg = json.loads(data)
        if msg.get("type") == "session.init":
            self.send_queue.put_nowait(json.dumps({"type": "session.created", "session_id": "sess_test"}))
        elif msg.get("type") == "input.append":
            self.received.append(msg)
        elif msg.get("type") == "session.close":
            self.closed = True

    async def close(self) -> None:
        pass


async def _fake_connect(url, **kw):
    del url, kw
    ws = FakeWs()
    ws.send_queue.put_nowait(json.dumps({"type": "session.queue_done"}))
    return ws


def test_bridge_uplink_chunks_1s(monkeypatch):
    """1s 块累积：喂 2.5s 音频应产生 2 个上行块（2 满块 + 0.5s 残留）"""
    import websockets

    async def fake_connect(url, **kw):
        del url, kw
        ws = FakeWs()
        ws.send_queue.put_nowait(json.dumps({"type": "session.queue_done"}))
        return ws

    monkeypatch.setattr(websockets, "connect", fake_connect)
    sent: list[int] = []

    async def fake_send_chunk(self, chunk: bytes) -> None:
        sent.append(len(chunk))

    monkeypatch.setattr(ApmBridge, "_send_chunk", fake_send_chunk)

    async def run() -> None:
        bridge = ApmBridge(on_audio_out=_noop, api_url="ws://fake")
        await bridge.start()
        await bridge.feed_pcm(b"\x00\x00" * 16000 * 2)   # 2s
        await bridge.feed_pcm(b"\x00\x00" * 8000)        # 0.5s
        await bridge.close()

    asyncio.run(run())
    assert len(sent) == 2, f"2 满块，实际 {sent}"
    assert sent[0] == 16000 * 2


async def _noop(*a, **k):
    return None


async def _fake_connect(url, **kw):
    del url, kw
    ws = FakeWs()
    ws.send_queue.put_nowait(json.dumps({"type": "session.queue_done"}))
    return ws


# ---------- 流式模式装配 ----------
def test_stream_mode_flag():
    """path=apm → session.stream_mode=True"""
    cfg = VoiceConfig(path="apm")
    from app.voice.session import VoiceSession
    from fastapi import WebSocket

    class FakeEngine:
        pass

    session = VoiceSession(None, "t", cfg, FakeEngine())  # type: ignore[arg-type]
    assert session.stream_mode is True
    assert cfg.path != "auto"
    # 默认 auto → 半双工
    cfg2 = VoiceConfig()
    s2 = VoiceSession(None, "t", cfg2, FakeEngine())  # type: ignore[arg-type]
    assert s2.stream_mode is False
