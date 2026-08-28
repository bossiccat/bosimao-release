"""ApmBridge 云端连接建立（握手）：排队 → session.init → session.created

从 apm_bridge.py 拆出（单文件 ≤300 行硬门禁）：连接/重握手逻辑独立成模块，
ApmBridge.start() 委托 connect_and_handshake()。重连（A8）即重新执行本流程——
会话级 re-sync（新 ws + 新 session，上下文不延续，与官方 probe 语义一致）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from typing import Any

logger = logging.getLogger(__name__)

HANDSHAKE_RECV_TIMEOUT_S = 15


async def connect_ws(api_url: str, token: str) -> Any:
    """建立 ws 连接（代理绕过 + 新旧版 websockets 头参数兼容）"""
    # 绕过系统代理：本机 Clash(127.0.0.1:7890) 未运行会劫持全部外连（2026-08-05 实测）
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        os.environ.pop(k, None)
    import websockets

    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        return await websockets.connect(
            api_url, ssl=ssl.create_default_context(), additional_headers=headers,
            open_timeout=20.0, max_size=16 * 1024 * 1024,
        )
    except TypeError:
        # 旧版 websockets 用 extra_headers
        return await websockets.connect(
            api_url, ssl=ssl.create_default_context(), extra_headers=headers,
            open_timeout=20.0, max_size=16 * 1024 * 1024,
        )


async def _recv_json(ws: Any, phase: str) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_RECV_TIMEOUT_S)
    return json.loads(raw)


async def connect_and_handshake(api_url: str, token: str, system_prompt: str) -> tuple[Any, str]:
    """连接 + 排队 + session.init → (ws, session_id)；任一步失败抛异常（ws 由调用方清理）"""
    ws = await connect_ws(api_url, token)
    # 排队 → 就绪
    while True:
        msg = await _recv_json(ws, "queue")
        if msg.get("type") in ("session.queue_done", "queue_done"):
            break
        if msg.get("type") == "error":
            raise RuntimeError(f"API 排队失败: {msg}")
    # 会话初始化
    await ws.send(json.dumps({
        "type": "session.init",
        "payload": {"system_prompt": system_prompt},
    }))
    while True:
        msg = await _recv_json(ws, "session")
        if msg.get("type") == "session.created":
            session_id = msg.get("session_id", "")
            logger.info("apm session created: %s", session_id)
            return ws, session_id
        if msg.get("type") == "error":
            raise RuntimeError(f"API 会话失败: {msg}")
