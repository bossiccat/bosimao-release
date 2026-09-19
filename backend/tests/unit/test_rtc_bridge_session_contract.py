"""rtc_bridge 当前会话 hello 与单一 sidecar owner 契约测试。"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest
import websockets

import rtc_bridge.server as server_module
from rtc_bridge.config import BridgeConfig
from rtc_bridge.server import BridgeServer

# 本文件所有 websockets.connect 都打本机（127.0.0.1 上由测试自己起的 bridge/server）。
# `websockets` 的 `proxy` 默认是 `True`（= 按环境代理），设了 HTTP_PROXY 的机器上
# loopback 请求会被交给代理 ⇒ **服务活着却连不上**（同 docs/OPS-003-live-test.md:98-100）。
# 故每处显式 `proxy=None`。契约锁：backend/tests/contract/test_loopback_probe_proxy_contract.py


class AcceptingRedemption:
    async def redeem(self, hello: dict[str, Any]) -> dict[str, Any]:
        return {
            "redeemed": True,
            **{key: hello[key] for key in (
                "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
            )},
            "expires_at": "2026-08-25T10:00:00Z",
        }


async def _start_server() -> tuple[BridgeServer, dict[str, Any], Any, int]:
    state: dict[str, Any] = {
        "sidecar_connected": False,
        "room_id": "",
        "device_id": "",
        "_session_ref": None,
    }
    bridge = BridgeServer(BridgeConfig(ws_port=0), state, redemption=AcceptingRedemption())
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return bridge, state, server, port


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", None),
        ("session_id", ""),
        ("session_id", "   "),
        ("device_id", None),
        ("device_id", ""),
        ("device_id", "   "),
        ("room_id", None),
        ("room_id", ""),
        ("room_id", "   "),
    ],
)
async def test_incomplete_session_hello_is_rejected_before_session_creation(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str | None,
) -> None:
    created = False

    class UnexpectedSession:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            nonlocal created
            created = True

    monkeypatch.setattr(server_module, "PeerVoiceSession", UnexpectedSession)
    bridge, state, server, port = await _start_server()
    hello = {
        "type": "hello",
        "session_id": "session-current",
        "device_id": "android-current",
        "room_id": "room-current",
    }
    if value is None:
        hello.pop(field)
    else:
        hello[field] = value

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            await ws.send(json.dumps(hello))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert response == {
                "type": "ctrl",
                "action": "exit",
                "reason": "invalid_session_hello",
            }
        await asyncio.sleep(0)
        assert created is False
        assert bridge._session is None
        assert state["sidecar_connected"] is False
        assert state["_session_ref"] is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_hello_without_generation_is_rejected_before_session_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方案 B：没有 generation 的 hello 绝不能创建可接收媒体的会话。"""
    created = False

    class UnexpectedSession:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            nonlocal created
            created = True

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server_module, "PeerVoiceSession", UnexpectedSession)
    bridge, state, server, port = await _start_server()
    hello = {
        "type": "hello",
        "session_id": "session-current",
        "device_id": "android-current",
        "room_id": "room-current",
    }

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            await ws.send(json.dumps(hello))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert response == {
                "type": "ctrl",
                "action": "exit",
                "reason": "invalid_session_hello",
            }
        await asyncio.sleep(0)
        assert created is False
        assert bridge._session is None
        assert state["sidecar_connected"] is False
        assert state["_session_ref"] is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_hello_without_proof_and_nonce_is_rejected_before_session_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方案 B：即使带 generation，缺 proof/nonce 的 localhost hello 也必须 fail-closed。"""
    created = False

    class UnexpectedSession:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            nonlocal created
            created = True

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server_module, "PeerVoiceSession", UnexpectedSession)
    bridge, state, server, port = await _start_server()
    hello = {
        "type": "hello",
        "session_id": "session-current",
        "device_id": "android-current",
        "room_id": "room-current",
        "generation": 13,
        "protocol_version": "1.0",
    }

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            await ws.send(json.dumps(hello))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert response == {
                "type": "ctrl",
                "action": "exit",
                "reason": "invalid_session_hello",
            }
        await asyncio.sleep(0)
        assert created is False
        assert bridge._session is None
        assert state["sidecar_connected"] is False
        assert state["_session_ref"] is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_candidate_start_failure_closes_candidate_and_preserves_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSession:
        instances: list["FailingSession"] = []

        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.closed = False
            self.__class__.instances.append(self)

        async def start(self) -> None:
            raise RuntimeError("start failed")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(server_module, "PeerVoiceSession", FailingSession)
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws_a:
            await ws_a.send(json.dumps({
                "type": "hello", "proof": "proof-a", "nonce": "nonce-a-current-1", "jti": "jti-a",
                "session_id": "session-a", "device_id": "device-a", "room_id": "room-a",
                "sidecar_user_id": "jax-pc-sidecar", "generation": 0, "protocol_version": "1.0",
                "audio_format": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1, "frame_ms": 20, "frame_bytes": 640},
            }))
            try:
                await asyncio.wait_for(ws_a.recv(), timeout=1)
            except websockets.exceptions.ConnectionClosed:
                pass
            except asyncio.TimeoutError:
                pass
        assert FailingSession.instances and FailingSession.instances[-1].closed is True
        assert bridge._ws is None
        assert bridge._session is None
        assert state["sidecar_connected"] is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_failed_candidate_does_not_overwrite_sdk_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRedemption:
        async def redeem(self, hello: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("reject")

    bridge, state, server, port = await _start_server()
    bridge._redemption = AcceptingRedemption()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws_a:
            hello = {"type": "hello", "proof": "proof-a", "nonce": "nonce-a-current", "jti": "jti-a", "session_id": "session-a", "device_id": "device-a", "room_id": "room-a", "sidecar_user_id": "jax-pc-sidecar", "generation": 0, "protocol_version": "1.0", "audio_format": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1, "frame_ms": 20, "frame_bytes": 640}}
            await ws_a.send(json.dumps(hello))
            await ws_a.recv()
            bridge._redemption = FailingRedemption()
            async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws_b:
                candidate = dict(hello, session_id="session-b", device_id="device-b", room_id="room-b", sdk_version="candidate-sdk")
                await ws_b.send(json.dumps(candidate))
                await ws_b.recv()
            assert "sidecar_sdk_version" not in state or state["sidecar_sdk_version"] == ""
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_redemption_succeeds_before_session_creation_and_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingRedemption:
        async def redeem(self, hello: dict[str, Any]) -> dict[str, Any]:
            events.append("redeem")
            return {
                "redeemed": True,
                **{key: hello[key] for key in (
                    "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
                )},
                "expires_at": "2026-08-25T10:00:00Z",
            }

    class RecordingSession:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            events.append("session")

        async def start(self) -> None:
            events.append("start")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(server_module, "PeerVoiceSession", RecordingSession)
    state = {
        "sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None,
    }
    bridge = BridgeServer(
        BridgeConfig(ws_port=0), state, redemption=RecordingRedemption()
    )
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    hello = {
        "type": "hello", "proof": "proof-current", "nonce": "nonce-current-1234",
        "jti": "jti-current", "session_id": "session-current",
        "device_id": "android-current", "room_id": "room-current",
        "sidecar_user_id": "jax-pc-sidecar", "generation": 13,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            await ws.send(json.dumps(hello))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5)) == {"type": "ready"}
            assert events == ["redeem", "session", "start"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [40111, 40112, 40113, 40914, 50303, "timeout", "bad_json"])
async def test_redemption_failure_never_creates_session_or_ready(
    monkeypatch: pytest.MonkeyPatch, failure: object,
) -> None:
    created = False

    class RejectingRedemption:
        async def redeem(self, hello: dict[str, Any]) -> dict[str, Any]:
            del hello
            raise RuntimeError(str(failure))

    class UnexpectedSession:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            nonlocal created
            created = True

    monkeypatch.setattr(server_module, "PeerVoiceSession", UnexpectedSession)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    bridge = BridgeServer(BridgeConfig(ws_port=0), state, redemption=RejectingRedemption())
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    hello = {
        "type": "hello", "proof": "proof-current", "nonce": "nonce-current-1234",
        "jti": "jti-current", "session_id": "session-current",
        "device_id": "android-current", "room_id": "room-current",
        "sidecar_user_id": "jax-pc-sidecar", "generation": 13,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            await ws.send(json.dumps(hello))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert response == {
                "type": "ctrl", "action": "exit", "reason": "hello_redemption_failed",
            }
        assert created is False
        assert bridge._session is None
        assert state["sidecar_connected"] is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_disconnect_cleanup_has_no_python_process_owner() -> None:
    bridge, state, server, port = await _start_server()
    source = inspect.getsource(server_module)
    assert "subprocess" not in source
    assert not hasattr(bridge, "_schedule_sidecar_respawn")
    assert not hasattr(bridge, "_spawn_sidecar")

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
            await ws.send(json.dumps({
                "type": "hello", "proof": "proof-current", "nonce": "nonce-current-1234",
                "jti": "jti-current", "session_id": "session-current",
                "device_id": "android-current", "room_id": "room-current",
                "sidecar_user_id": "jax-pc-sidecar", "generation": 13,
                "protocol_version": "1.0",
                "audio_format": {
                    "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
                    "frame_ms": 20, "frame_bytes": 640,
                },
            }))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5))["type"] == "ready"
        for _ in range(50):
            if bridge._ws is None:
                break
            await asyncio.sleep(0.01)
        assert bridge._ws is None
        assert bridge._session is None
        assert state["sidecar_connected"] is False
    finally:
        server.close()
        await server.wait_closed()
