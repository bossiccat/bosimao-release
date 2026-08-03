"""事件总线：检测结果 → 提醒/推送/UI 广播 的解耦通道"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class EventBus:
    """异步事件总线：emit 事件，所有订阅 handler 并发执行"""

    _handlers: dict[str, list[Handler]] = field(default_factory=dict)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        if event_type in self._handlers and handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """触发事件（并发执行所有订阅者；单个 handler 异常不影响其他）"""
        for handler in self._handlers.get(event_type, []):
            try:
                await handler(data)
            except Exception:  # noqa: BLE001 - 事件隔离，不允许一个订阅者拖垮总线
                import logging

                logging.getLogger(__name__).exception(
                    "event handler failed: type=%s handler=%s", event_type, handler
                )


# 事件类型常量（与 openapi.yaml WS 契约一致）
EVT_SESSION_UPDATED = "session_updated"
EVT_ALERT = "alert"
EVT_PET_STATE = "pet_state"
EVT_VOICE_TRANSCRIPT = "voice_transcript"
