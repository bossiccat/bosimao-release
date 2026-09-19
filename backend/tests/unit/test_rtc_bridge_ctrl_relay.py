"""sidecar → rtc_bridge WS ctrl「note_termination」中继行为测试。

中继链路（方案 A）：terminate 调用方获知 tid → （上游接入点）→ sidecar 经既有
bridge WS 上行 {type:'ctrl', action:'note_termination', session_id,
termination_id} → rtc_bridge 校验当前活动会话匹配后注入注册表 → drain 时真实上报。
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
import websockets

from rtc_bridge.config import BridgeConfig
from rtc_bridge.server import BridgeServer

# 本文件所有 websockets.connect 都打本机（127.0.0.1 上由测试自己起的 bridge/server）。
# `websockets` 的 `proxy` 默认是 `True`（= 按环境代理），设了 HTTP_PROXY 的机器上
# loopback 请求会被交给代理 ⇒ **服务活着却连不上**（同 docs/OPS-003-live-test.md:98-100）。
# 故每处显式 `proxy=None`。契约锁：backend/tests/contract/test_loopback_probe_proxy_contract.py

logging.disable(logging.WARNING)


class FakeRedemption:
    async def redeem(self, hello: dict) -> dict:
        return {
            "redeemed": True,
            **{key: hello[key] for key in (
                "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
            )},
            "expires_at": "2026-08-25T10:00:00Z",
        }


class RecordingAckReporter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def report_drained_closed(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"ack_result": "confirmed"}


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


def _relay_msg(session_id: str, termination_id: str) -> str:
    return json.dumps({
        "type": "ctrl", "action": "note_termination",
        "session_id": session_id, "termination_id": termination_id,
    })


async def _start_server(reporter):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    bridge = BridgeServer(cfg, state, redemption=FakeRedemption(), ack_reporter=reporter)
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return bridge, state, server, port


async def _connect_sidecar(port: int, session_id: str, device_id: str, room_id: str):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}", proxy=None)
    await ws.send(json.dumps(_hello(session_id, device_id, room_id)))
    ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert ready["type"] == "ready"
    return ws


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_ctrl_relay_injects_context_and_drain_reports():
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-001", "dev-001", "jax-dev-001")
        await ws.send(_relay_msg("session-001", "tid-via-relay"))
        await asyncio.sleep(0.2)
        assert bridge._terminations.get("session-001") == "tid-via-relay"
        await ws.close()
        assert await _wait_for(lambda: len(reporter.calls) == 1)
        assert reporter.calls[0]["termination_id"] == "tid-via-relay"
        assert reporter.calls[0]["result"] == "confirmed"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ctrl_relay_rejects_session_mismatch():
    """中继声明的 session 与当前活动会话不一致 → 不注入、drain 不上报"""
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-001", "dev-001", "jax-dev-001")
        await ws.send(_relay_msg("session-other", "tid-x"))
        await asyncio.sleep(0.2)
        assert bridge._terminations.get("session-other") is None
        await ws.close()
        await asyncio.sleep(0.3)
        assert reporter.calls == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ctrl_relay_malformed_payloads_never_crash_or_inject():
    """畸形载荷（缺字段/非字符串/超长/未知动作/非 JSON）→ 忽略且连接保持可用"""
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-001", "dev-001", "jax-dev-001")
        malformed = [
            "not-json-at-all",
            json.dumps({"type": "ctrl", "action": "note_termination"}),
            json.dumps({"type": "ctrl", "action": "note_termination",
                        "session_id": "", "termination_id": "t"}),
            json.dumps({"type": "ctrl", "action": "note_termination",
                        "session_id": "session-001", "termination_id": ""}),
            json.dumps({"type": "ctrl", "action": "note_termination",
                        "session_id": 7, "termination_id": ["t"]}),
            json.dumps({"type": "ctrl", "action": "note_termination",
                        "session_id": "session-001", "termination_id": "t" * 129}),
            json.dumps({"type": "ctrl", "action": "unknown_action", "reason": "r"}),
            json.dumps({"type": "up_audio"}),  # 缺 pcm_b64 的既有消息也不崩
        ]
        for raw in malformed:
            await ws.send(raw)
        await asyncio.sleep(0.2)
        assert bridge._terminations.get("session-001") is None
        # 连接仍可用：合法中继随后照常生效
        await ws.send(_relay_msg("session-001", "tid-after-malformed"))
        await asyncio.sleep(0.2)
        assert bridge._terminations.get("session-001") == "tid-after-malformed"
        await ws.close()
        assert await _wait_for(lambda: len(reporter.calls) == 1)
        assert reporter.calls[0]["termination_id"] == "tid-after-malformed"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_public_note_termination_seam_still_works():
    """#21 注入接缝回归：进程内 note_termination 仍驱动 drain 上报"""
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-a", "dev-a", "r-a")
        bridge.note_termination("session-a", "tid-direct")
        await ws.close()
        assert await _wait_for(lambda: len(reporter.calls) == 1)
        assert reporter.calls[0]["termination_id"] == "tid-direct"
    finally:
        server.close()
        await server.wait_closed()
