"""F6/F7 下行逐帧关联观测（RED 先行）

目的：证明「云端吐出的某一帧」与「发给 sidecar 的某一帧」是同一帧。
没有 reply_id / frame_seq / src_seq 时，日志里只有孤立的字节数，
无法把 qwen delta（L0）→ bridge 成帧（L1）→ sidecar 收帧（L2）→
sendCustomAudioData（L3）串成因果链，也就无法定位"卡断"到底断在哪一跳。

设计：reply 身份在 bridge 本地铸造（不改动 ApmBridge / QwenRealtimeBridge
回调签名），元数据随帧走完 shaper → 有界队列 → WS → sidecar。
新增字段全部可选，旧 sidecar 忽略未知字段即可正常工作。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import pytest
import websockets

from rtc_bridge.config import BridgeConfig
from rtc_bridge.server import BridgeServer
from rtc_bridge.session import PeerVoiceSession
from rtc_bridge.shaper import DownlinkShaper

logging.disable(logging.WARNING)

FRAME = b"\xab\xcd" * 320  # 640B = 20ms @16k s16 mono


class FakeApm:
    def __init__(self, on_audio_out=None, on_text=None, on_state=None, **kwargs) -> None:
        self.on_audio_out = on_audio_out
        self.on_text = on_text
        self.on_state = on_state
        self.fed: list[bytes] = []
        self.closed = False
        self.started = False

    async def feed_pcm(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def close(self) -> None:
        self.closed = True

    async def start(self) -> None:
        self.started = True


class FakeRedemption:
    async def redeem(self, hello: dict) -> dict:
        return {"session_id": hello["session_id"], "device_id": hello["device_id"]}


def _hello(session_id: str, device_id: str, room_id: str) -> dict:
    return {
        "type": "hello", "proof": f"proof-{session_id}",
        "nonce": f"nonce-{session_id}-current", "jti": f"jti-{session_id}",
        "session_id": session_id, "device_id": device_id, "room_id": room_id,
        "sidecar_user_id": "jax-pc-sidecar", "generation": 0,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }


async def _start_server():
    cfg = BridgeConfig(ws_port=0, voice_engine="apm")
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    bridge = BridgeServer(cfg, state, redemption=FakeRedemption())
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return bridge, state, server, port


@pytest.fixture
def fake_apm(monkeypatch):
    import rtc_bridge.session as session_mod

    instances: list[FakeApm] = []

    def factory(*args, **kwargs):
        inst = FakeApm(*args, **kwargs)
        instances.append(inst)
        return inst

    monkeypatch.setattr(session_mod, "ApmBridge", factory)
    return instances


# ---------------- shaper 层：帧元数据 ----------------


@pytest.mark.asyncio
async def test_shaper_emits_reply_id_and_frame_seq():
    """同一 reply 内 frame_seq 单调递增；跨 chunk 不重置"""
    got: list = []

    async def send_frame(frame) -> None:
        got.append(frame)

    shaper = DownlinkShaper(send_frame=send_frame, frame_ms=20, sample_rate=16000)
    shaper.start()
    shaper.begin_reply("s1:0:1")
    await shaper.push(FRAME, src_seq=0)
    await shaper.push(b"\x02" * 1280, src_seq=1)  # 一个 chunk 出两帧
    await asyncio.sleep(0.2)
    await shaper.stop()

    assert [f.frame_seq for f in got] == [0, 1, 2], "frame_seq 必须在 reply 内单调递增"
    assert [f.src_seq for f in got] == [0, 1, 1], "同一 chunk 产出的多帧共享 src_seq"
    assert all(f.reply_id == "s1:0:1" for f in got)
    assert all(len(f.payload) == 640 for f in got)


@pytest.mark.asyncio
async def test_shaper_frame_seq_resets_on_new_reply():
    """新 reply 开始 → frame_seq 归零，reply_id 切换"""
    got: list = []

    async def send_frame(frame) -> None:
        got.append(frame)

    shaper = DownlinkShaper(send_frame=send_frame, frame_ms=20, sample_rate=16000)
    shaper.start()
    shaper.begin_reply("s1:0:1")
    await shaper.push(FRAME, src_seq=0)
    await asyncio.sleep(0.1)
    shaper.begin_reply("s1:0:2")
    await shaper.push(FRAME, src_seq=0)
    await asyncio.sleep(0.1)
    await shaper.stop()

    assert [f.reply_id for f in got] == ["s1:0:1", "s1:0:2"]
    assert [f.frame_seq for f in got] == [0, 0], "新 reply 必须重置 frame_seq"


@pytest.mark.asyncio
async def test_shaper_push_without_meta_still_works():
    """向后兼容：不传 reply_id/src_seq 时仍正常出帧（旧调用点不破）"""
    got: list = []

    async def send_frame(frame) -> None:
        got.append(frame)

    shaper = DownlinkShaper(send_frame=send_frame, frame_ms=20, sample_rate=16000)
    shaper.start()
    await shaper.push(FRAME)
    await asyncio.sleep(0.15)
    await shaper.stop()

    assert len(got) == 1
    assert len(got[0].payload) == 640
    assert got[0].frame_seq == 0


# ---------------- session 层：WS 消息携带追溯字段 ----------------


@pytest.mark.asyncio
async def test_down_audio_carries_trace_fields(fake_apm):
    """down_audio 必须带 reply_id / frame_seq / src_seq / t_enq / t_send"""
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-001", "dev-001", "jax-dev-001")))
            await ws.recv()  # ready
            await fake_apm[0].on_audio_out(FRAME)
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

            assert msg["type"] == "down_audio"
            assert base64.b64decode(msg["pcm_b64"]) == FRAME, "pcm_b64 契约不得破坏"
            assert msg["reply_id"].startswith("session-001:"), \
                f"reply_id 必须含 session_id，实测 {msg['reply_id']!r}"
            assert msg["frame_seq"] == 0
            assert msg["src_seq"] == 0
            assert isinstance(msg["t_enq"], float) and msg["t_enq"] > 0
            assert isinstance(msg["t_send"], float) and msg["t_send"] >= msg["t_enq"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_consecutive_chunks_share_reply_id(fake_apm):
    """同一轮回复的连续 chunk → 同一 reply_id，frame_seq 递增"""
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-002", "dev-002", "jax-dev-002")))
            await ws.recv()  # ready
            await fake_apm[0].on_audio_out(FRAME)
            await fake_apm[0].on_audio_out(b"\x02" * 640)
            m1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            m2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

            assert m1["reply_id"] == m2["reply_id"], "同轮回复必须共享 reply_id"
            assert (m1["frame_seq"], m2["frame_seq"]) == (0, 1)
            assert (m1["src_seq"], m2["src_seq"]) == (0, 1)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_new_reply_after_gap_gets_new_reply_id(fake_apm):
    """下行静默超过阈值后再来音频 → 铸造新 reply_id（区分两轮回复）"""
    sent: list[dict] = []

    async def send_msg(msg: dict) -> None:
        sent.append(msg)

    session = PeerVoiceSession(
        device_id="dev-x", room_id="room-x", send_msg=send_msg,
        apm_api_url="", apm_system_prompt="", session_id="session-003",
        new_reply_gap_s=0.05,
    )
    session._build_apm()
    await session.start()
    try:
        await session._on_audio_out(FRAME)
        await asyncio.sleep(0.15)
        await asyncio.sleep(0.15)  # 累计静默 > gap(0.05) → 下一帧判为新 reply
        await session._on_audio_out(b"\x03" * 640)
        await asyncio.sleep(0.15)

        assert len(sent) == 2, f"应恰好下发 2 帧，实测 {len(sent)}"
        assert sent[0]["reply_id"] != sent[1]["reply_id"], \
            "跨静默间隔的两轮回复必须分配不同 reply_id"
        assert sent[1]["frame_seq"] == 0, "新 reply 的首帧 frame_seq 必须为 0"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_id_defaults_to_empty_without_injection():
    """未注入 session_id 时 reply_id 仍可生成（不因缺参崩溃）"""
    sent: list[dict] = []

    async def send_msg(msg: dict) -> None:
        sent.append(msg)

    session = PeerVoiceSession(
        device_id="dev-y", room_id="room-y", send_msg=send_msg,
        apm_api_url="", apm_system_prompt="",
    )
    session._build_apm()
    await session.start()
    try:
        await session._on_audio_out(FRAME)
        await asyncio.sleep(0.15)
        assert sent and isinstance(sent[0]["reply_id"], str)
    finally:
        await session.close()
