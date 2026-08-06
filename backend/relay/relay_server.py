"""WebSocket 中继服务（M2，独立于 app 可单独启动，端口 19090）

- 纯透传：只按 (pairing_code, role) 配对并把帧原样双向转发，不解析不存储音频内容
- 配对：手机/PC 各自发 pair 帧（同一 pairing_code）→ 双向绑定；对端断开发 peer_left
- 鉴权：RELAY_TOKEN（未配置 → 开发态放行）；心跳：15s ping / 60s 无帧踢连接并通知对端
- 音频帧（二进制）整体透传，E2EE 时中继只见密文

入口：python -m backend.relay.relay_server（或 uvicorn backend.relay.relay_server:app --port 19090）
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket

from .config import RELAY_PATH, RelayConfig, load_relay_config
from .relay_protocol import make_error, parse_pair_frame

logger = logging.getLogger(__name__)


class RelayConn:
    """单个中继连接（手机或 PC）"""

    def __init__(self, ws: WebSocket, role: str, device_id: str, pairing_code: str, token: str) -> None:
        self.ws = ws
        self.role = role
        self.device_id = device_id
        self.pairing_code = pairing_code
        self.token = token
        self.peer: RelayConn | None = None
        self.session_id = ""
        self.last_rx = time.time()
        self.created_at = time.time()
        self.closed = False

    async def send_text(self, raw: str) -> None:
        await self.ws.send_text(raw)

    async def send_json(self, obj: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)


class RelayServer:
    """连接注册表 + 配对 + 转发 + 心跳清理（纯逻辑，可单测）"""

    def __init__(self, cfg: RelayConfig) -> None:
        self.cfg = cfg
        self._conns: dict[tuple[str, str], RelayConn] = {}
        self._lock = asyncio.Lock()
        self.stats = {"paired": 0, "forwarded": 0, "kicked": 0, "replays": 0}

    # ---------- 注册 / 配对 ----------
    async def register(self, conn: RelayConn) -> tuple[RelayConn | None, bool]:
        """注册连接；若对端已在等待 → 建立会话并返回 (peer, True)"""
        async with self._lock:
            key = (conn.pairing_code, conn.role)
            old = self._conns.get(key)
            if old is not None and old is not conn and not old.closed:
                self.stats["kicked"] += 1
                await self._safe_close(old, 1001, "replaced by new connection")
            self._conns[key] = conn
            peer_key = (conn.pairing_code, "pc" if conn.role == "phone" else "phone")
            peer = self._conns.get(peer_key)
            if peer is not None and not peer.closed:
                conn.peer = peer
                peer.peer = conn
                session_id = f"rs-{int(time.time())}-{conn.pairing_code}"
                conn.session_id = peer.session_id = session_id
                self.stats["paired"] += 1
                for c, other in ((conn, peer), (peer, conn)):
                    await c.send_json({"type": "paired", "session_id": session_id,
                                       "peer": {"role": other.role, "device_id": other.device_id}})
                return peer, True
        return None, False

    async def unregister(self, conn: RelayConn) -> None:
        async with self._lock:
            key = (conn.pairing_code, conn.role)
            if self._conns.get(key) is conn:
                self._conns.pop(key, None)
        peer = conn.peer
        conn.closed = True
        conn.peer = None
        if peer is not None and not peer.closed:
            peer.peer = None
            try:
                await peer.send_json({"type": "peer_left", "session_id": conn.session_id,
                                      "device_id": conn.device_id})
            except Exception:  # noqa: BLE001 - 对端可能同时断开
                pass

    # ---------- 转发 ----------
    async def forward(self, conn: RelayConn, raw_text: str | None = None, raw_bytes: bytes | None = None) -> None:
        """把帧转发给对端（纯透传不解析内容）；只拦截中继自身心跳，其余原样转发"""
        if raw_text is not None:
            try:
                msg = json.loads(raw_text)
            except json.JSONDecodeError:
                pass  # 无法解析的控制帧仍原样透传（不解析内容）
            else:
                # 拦截心跳相关帧：heartbeat/pong 均不转发（pong 是对中继 ping 的响应，
                # 若当普通帧转发且 peer=None 会给客户端回 error("no_peer")——2026-08-05 修复）
                if msg.get("type") in ("heartbeat", "pong"):
                    await conn.send_json({"type": "pong", "ts": msg.get("ts", time.time())})
                    return
        peer = conn.peer
        if peer is None or peer.closed:
            if raw_text is not None:
                await conn.send_text(make_error("no_peer", "对端未连接"))
            return
        try:
            if raw_bytes is not None:
                await peer.send_bytes(raw_bytes)
            else:
                await peer.send_text(raw_text)
            self.stats["forwarded"] += 1
        except Exception:  # noqa: BLE001 - 对端断开
            await self.unregister(peer)

    # ---------- 心跳 / 清理 ----------
    async def heartbeat_loop(self, conn: RelayConn) -> None:
        while not conn.closed:
            await asyncio.sleep(self.cfg.heartbeat_interval_s)
            if conn.closed:
                break
            try:
                await conn.send_json({"type": "ping", "ts": time.time()})
            except Exception:  # noqa: BLE001
                break
            if time.time() - conn.last_rx > self.cfg.heartbeat_timeout_s:
                self.stats["kicked"] += 1
                await self._safe_close(conn, 1001, "heartbeat timeout")
                break
            # pair timeout 仅对 phone 角色生效：手机是临时连接（不配对即清理）；
            # PC 是常驻角色（relay_client 常驻等待手机），绝不能踢——否则 PC 永远
            # 无法保持在线，手机随时配对都会"对端不在线→静默"（2026-08-05 修复）
            if (conn.peer is None and conn.role == "phone"
                    and time.time() - conn.created_at > self.cfg.session_timeout_s):
                self.stats["kicked"] += 1
                await self._safe_close(conn, 1001, "pair timeout")
                break

    @staticmethod
    async def _safe_close(conn: RelayConn, code: int, reason: str) -> None:
        conn.closed = True
        try:
            await conn.ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "online": len(self._conns),
            "stats": dict(self.stats),
            "sessions": [
                {"pairing_code": c.pairing_code, "role": c.role, "device_id": c.device_id,
                 "peer": c.peer.device_id if c.peer else None, "session_id": c.session_id}
                for c in self._conns.values()
            ],
        }


async def accept_conn(ws: WebSocket, cfg: RelayConfig, server: RelayServer) -> RelayConn | None:
    """握手：accept → 等 pair 帧 → token 校验 → 建 RelayConn；失败返回 None"""
    await ws.accept()
    query_token = (ws.query_params or {}).get("token", "")
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=cfg.heartbeat_timeout_s)
    except Exception:  # noqa: BLE001 - 超时/断开/二进制帧
        await ws.close(code=1008, reason="pair timeout")
        return None
    try:
        msg = parse_pair_frame(raw)
    except ValueError as e:
        await ws.send_text(make_error("bad_frame", str(e)))
        await ws.close(code=1008, reason="bad pair frame")
        return None
    token = query_token or msg.get("token", "")
    if cfg.require_token and token != cfg.token:
        await ws.send_text(make_error("auth_failed", "token 无效"))
        await ws.close(code=1008, reason="auth failed")
        return None
    return RelayConn(ws, msg["role"], msg["device_id"], msg["pairing_code"], token)


async def run_conn(conn: RelayConn, server: RelayServer) -> None:
    """连接主循环：注册配对 → 收帧转发；断线清理"""
    await server.register(conn)
    hb = asyncio.create_task(server.heartbeat_loop(conn))
    try:
        while True:
            msg = await conn.ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] == "websocket.receive":
                conn.last_rx = time.time()
                if msg.get("text") is not None:
                    await server.forward(conn, raw_text=msg["text"])
                elif msg.get("bytes") is not None:
                    await server.forward(conn, raw_bytes=msg["bytes"])
    except Exception:  # noqa: BLE001 - 断开/异常统一清理
        pass
    finally:
        hb.cancel()
        await server.unregister(conn)


def create_relay_app(cfg: RelayConfig | None = None) -> FastAPI:
    """装配中继应用（入口调用；测试可传自建 cfg）"""
    cfg = cfg or load_relay_config()
    server = RelayServer(cfg)
    router = APIRouter(tags=["relay"])

    @router.websocket(RELAY_PATH)
    async def relay_ws(ws: WebSocket) -> None:
        conn = await accept_conn(ws, cfg, server)
        if conn is None:
            return
        await run_conn(conn, server)

    @router.post("/relay/pair")
    async def relay_pair() -> dict:
        """控制面：生成配对码 + 返回连接信息（配对主流程走 WS pair 帧）"""
        code = f"{secrets.randbelow(1000000):06d}"
        return {"code": 0, "data": {
            "pairing_code": code, "ws_url": f"ws://{{host}}:{cfg.port}{RELAY_PATH}",
            "token_required": cfg.require_token, "e2ee_enabled": cfg.e2ee_enabled,
            "ts": int(time.time()),
        }, "message": "配对码已签发"}

    @router.get("/relay/stats")
    async def relay_stats() -> dict:
        return {"code": 0, "data": server.status(), "message": ""}

    app = FastAPI(title="贾克斯模式 - 中继服务", version="1.5-m2")
    app.include_router(router)

    @app.get("/relay/health")
    async def relay_health() -> dict:
        return {"status": "ok", "port": cfg.port}

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    cfg = load_relay_config()
    app = create_relay_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    run()
