"""APM websocket handshake and session initialization."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from typing import Any

logger = logging.getLogger(__name__)


async def open_session(api_url: str, token: str, system_prompt: str) -> tuple[Any, str]:
    """Connect to the realtime API and complete queue/session handshakes."""
    # The local development proxy can intercept the realtime websocket.
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        os.environ.pop(key, None)

    import websockets

    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        ws = await websockets.connect(
            api_url,
            ssl=ssl.create_default_context(),
            additional_headers=headers,
            open_timeout=20.0,
            max_size=16 * 1024 * 1024,
        )
    except TypeError:
        # Compatibility with websockets releases using the old keyword.
        ws = await websockets.connect(
            api_url,
            ssl=ssl.create_default_context(),
            extra_headers=headers,
            open_timeout=20.0,
            max_size=16 * 1024 * 1024,
        )

    try:
        await _wait_for(ws, {"session.queue_done", "queue_done"}, "API 排队失败")
        await ws.send(json.dumps({"type": "session.init", "payload": {"system_prompt": system_prompt}}))
        message = await _wait_for(ws, {"session.created"}, "API 会话失败", return_message=True)
        session_id = message.get("session_id", "")
        logger.info("apm session created: %s", session_id)
        return ws, session_id
    except Exception:
        await _close_quietly(ws)
        raise


async def _wait_for(ws: Any, accepted: set[str], error_prefix: str, *, return_message: bool = False) -> dict[str, Any]:
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if message.get("type") in accepted:
            return message if return_message else message
        if message.get("type") == "error":
            raise RuntimeError(f"{error_prefix}: {message}")


async def _close_quietly(ws: Any) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
