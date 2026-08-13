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


async def _start_server() -> tuple[BridgeServer, dict[str, Any], Any, int]:
    state: dict[str, Any] = {
        "sidecar_connected": False,
        "room_id": "",
        "device_id": "",
        "_session_ref": None,
    }
    bridge = BridgeServer(BridgeConfig(ws_port=0), state)
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
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
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
async def test_disconnect_cleanup_has_no_python_process_owner() -> None:
    bridge, state, server, port = await _start_server()
    source = inspect.getsource(server_module)
    assert "subprocess" not in source
    assert not hasattr(bridge, "_schedule_sidecar_respawn")
    assert not hasattr(bridge, "_spawn_sidecar")

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "session_id": "session-current",
                "device_id": "android-current",
                "room_id": "room-current",
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
