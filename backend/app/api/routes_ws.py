"""WebSocket /ws/pet：状态推送 + 指令下行 + 心跳"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.events import EVT_ALERT, EVT_SESSION_UPDATED, EventBus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])


class WsHub:
    """WS 连接管理器：向所有已连接 UI 广播事件"""

    def __init__(self, bus: EventBus) -> None:
        self._connections: set[WebSocket] = set()
        self._bus = bus
        self._bus.subscribe(EVT_SESSION_UPDATED, self._broadcast_session)
        self._bus.subscribe(EVT_ALERT, self._broadcast_alert)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.info("ws connected: total=%d", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                self._connections.discard(ws)

    async def _broadcast_session(self, data: dict) -> None:
        await self.broadcast({"type": "event", "event": "session_updated", "data": data})

    async def _broadcast_alert(self, data: dict) -> None:
        await self.broadcast({"type": "event", "event": "alert", "data": data})


def create_ws_router(bus: EventBus) -> tuple[APIRouter, WsHub]:
    hub = WsHub(bus)

    @router.websocket("/ws/pet")
    async def ws_pet(ws: WebSocket) -> None:
        await hub.connect(ws)
        try:
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "ping":
                    await ws.send_json({"type": "pong", "ts": msg.get("ts", time.time())})
                elif mtype == "control":
                    # 控制指令转发（由 main 注入的处理器处理，MVP 直接 echo 受理）
                    await ws.send_json({"type": "ack", "action": msg.get("action")})
        except WebSocketDisconnect:
            hub.disconnect(ws)
        except Exception:  # noqa: BLE001
            logger.exception("ws error")
            hub.disconnect(ws)

    return router, hub
