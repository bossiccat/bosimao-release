"""APM websocket reconnect policy."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def reconnect(
    *,
    lock: asyncio.Lock,
    is_closed: Callable[[], bool],
    get_ws: Callable[[], Any],
    set_ws: Callable[[Any], None],
    get_recv_task: Callable[[], asyncio.Task | None],
    set_recv_task: Callable[[asyncio.Task | None], None],
    start: Callable[[], Awaitable[None]],
) -> None:
    """Close the stale connection and establish a fresh initialized session."""
    async with lock:
        if is_closed():
            return
        ws = get_ws()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        set_ws(None)
        recv_task = get_recv_task()
        if recv_task is not None:
            recv_task.cancel()
            set_recv_task(None)
        try:
            await start()
        except Exception as exc:  # noqa: BLE001
            logger.error("apm reconnect failed: %s", exc)
            set_ws(None)
