"""rtc_bridge 的 apm_cancelled_closed 上报行为测试。

ACK_REPORTERS（control_plane_base.py）中 apm_cancelled_closed 唯一合法
reporter 是 rtc_bridge。触发语义：桥持有的 APM 会话被取消并关闭（远端离开
释放 APM / 会话拆除），且存在终止上下文时，恰好一次上报；失败不冒泡；
与 bridge_drained_closed（drain 上报）互不重复。
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

logging.disable(logging.WARNING)


class FakeApm:
    """可控假 ApmBridge：feed 激活 started；可配置 close 抛错"""

    instances: list["FakeApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, on_state=None, **kwargs) -> None:
        self.on_audio_out = on_audio_out
        self.fed: list[bytes] = []
        self.closed = False
        self.started = False
        self.raise_on_close = False
        FakeApm.instances.append(self)

    async def feed_pcm(self, pcm: bytes) -> None:
        self.started = True   # 对齐真实现：feed_pcm 内懒初始化成功即视为激活
        self.fed.append(pcm)

    async def close(self) -> None:
        if self.raise_on_close:
            raise RuntimeError("apm wedged")
        self.closed = True


@pytest.fixture
def apm_instances(monkeypatch):
    import rtc_bridge.session as session_mod

    FakeApm.instances = []

    def factory(*args, **kwargs):
        return FakeApm(*args, **kwargs)

    monkeypatch.setattr(session_mod, "ApmBridge", factory)
    return FakeApm.instances


class RecordingAckReporter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def report_drained_closed(self, **kwargs) -> dict:
        self.calls.append({"ack": "bridge_drained_closed", **kwargs})
        return {}

    async def report_apm_cancelled_closed(self, **kwargs) -> dict:
        self.calls.append({"ack": "apm_cancelled_closed", **kwargs})
        return {}


class BoomAckReporter:
    async def report_drained_closed(self, **kwargs) -> dict:
        raise RuntimeError("cp unreachable")

    async def report_apm_cancelled_closed(self, **kwargs) -> dict:
        raise RuntimeError("cp unreachable")


class FakeRedemption:
    async def redeem(self, hello: dict) -> dict:
        return {
            "redeemed": True,
            **{key: hello[key] for key in (
                "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
            )},
            "expires_at": "2026-08-25T10:00:00Z",
        }


def _hello(session_id: str) -> dict:
    return {
        "type": "hello", "proof": f"proof-{session_id}",
        "nonce": f"nonce-{session_id}-current", "jti": f"jti-{session_id}",
        "session_id": session_id, "device_id": f"dev-{session_id}",
        "room_id": f"room-{session_id}",
        "sidecar_user_id": "jax-pc-sidecar", "generation": 0,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }


async def _start_server(reporter):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    bridge = BridgeServer(cfg, state, redemption=FakeRedemption(), ack_reporter=reporter)
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return bridge, state, server, port


async def _connect(port: int, session_id: str):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await ws.send(json.dumps(_hello(session_id)))
    ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert ready["type"] == "ready"
    return ws


async def _activate_apm(ws) -> None:
    """喂一帧有声 PCM 触发 APM 懒初始化（对齐 EndDetectFeeder 直通路径）"""
    pcm = b"\x30\x31" * 3200  # 非零 → RMS 高于静音阈值 → 直通 apm.feed_pcm
    await ws.send(json.dumps({
        "type": "up_audio", "pcm_b64": base64.b64encode(pcm).decode(),
    }))
    await asyncio.sleep(0.25)


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_apm_cancel_reports_once_and_drain_reports_distinctly(apm_instances):
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect(port, "session-001")
        await _activate_apm(ws)
        bridge.note_termination("session-001", "tid-1")
        # 远端离开 → 释放 APM 会话 → apm_cancelled_closed 恰好一次
        await ws.send(json.dumps({"type": "peer_state", "state": "leave", "user_id": "u1"}))
        assert await _wait_for(
            lambda: len([c for c in reporter.calls
                         if c["ack"] == "apm_cancelled_closed"]) == 1
        )
        # 连接关闭 → drain 上报 bridge_drained_closed，两者互不重复
        await ws.close()
        assert await _wait_for(lambda: len(reporter.calls) == 2)
        acks = [c["ack"] for c in reporter.calls]
        assert sorted(acks) == ["apm_cancelled_closed", "bridge_drained_closed"]
        apm_call = next(c for c in reporter.calls if c["ack"] == "apm_cancelled_closed")
        assert apm_call["termination_id"] == "tid-1"
        assert apm_call["result"] == "confirmed"
        assert apm_call["error_code"] is None
        drain_call = next(c for c in reporter.calls if c["ack"] == "bridge_drained_closed")
        assert drain_call["termination_id"] == "tid-1"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_apm_cancel_without_context_is_skipped(apm_instances):
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect(port, "session-a")
        await _activate_apm(ws)
        await ws.send(json.dumps({"type": "peer_state", "state": "leave", "user_id": "u"}))
        await ws.close()
        await asyncio.sleep(0.4)
        assert reporter.calls == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_inactive_apm_never_reports_even_with_context(apm_instances):
    """APM 从未激活（无音频喂入）→ 无取消事实，不上报"""
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect(port, "session-b")
        bridge.note_termination("session-b", "tid-b")
        await ws.send(json.dumps({"type": "peer_state", "state": "leave", "user_id": "u"}))
        await ws.close()
        await asyncio.sleep(0.4)
        # APM 无取消事实 → 不报 apm_cancelled_closed（drain 上报是既有行为，不受影响）
        assert [c for c in reporter.calls if c["ack"] == "apm_cancelled_closed"] == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_apm_ack_failure_never_breaks_voice_path(apm_instances):
    reporter = BoomAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect(port, "session-c")
        await _activate_apm(ws)
        bridge.note_termination("session-c", "tid-c")
        await ws.send(json.dumps({"type": "peer_state", "state": "leave", "user_id": "u"}))
        await asyncio.sleep(0.3)
        # 主链路不受影响：会话仍存活、清理正常、新连接可用
        assert state["sidecar_connected"] is True
        await ws.close()
        await asyncio.sleep(0.2)
        assert state["sidecar_connected"] is False
        ws2 = await _connect(port, "session-d")
        assert state["sidecar_connected"] is True
        await ws2.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unclean_apm_close_reports_failed_with_error_code(apm_instances):
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect(port, "session-e")
        await _activate_apm(ws)
        apm_instances[-1].raise_on_close = True
        bridge.note_termination("session-e", "tid-e")
        await ws.send(json.dumps({"type": "peer_state", "state": "leave", "user_id": "u"}))
        assert await _wait_for(
            lambda: len([c for c in reporter.calls
                         if c["ack"] == "apm_cancelled_closed"]) == 1
        )
        call = next(c for c in reporter.calls if c["ack"] == "apm_cancelled_closed")
        assert call["result"] == "failed"
        assert call["error_code"] == "apm_close_failed"
    finally:
        server.close()
        await server.wait_closed()
