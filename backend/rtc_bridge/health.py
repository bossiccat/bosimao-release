"""HealthServer —— rtc_bridge 健康检查（127.0.0.1:19093，asyncio 原生 HTTP，零依赖）

- GET /health  → 200 {status:"ok", sidecar_connected, rooms}（待命态也算健康，避免看门狗误杀）
- GET /metrics → 200 指标（rooms / sidecar_connected / up_frames / down_frames /
                  last_peer_ts / apm_session_state / sidecar_sdk_version）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class HealthServer:
    """极简 HTTP 服务（asyncio.start_server），供 jax-watchdog / backend status 轮询"""

    def __init__(self, host: str, port: int, state: dict, on_test_audio=None,
                 *, test_audio_enabled: bool = False) -> None:
        self.host = host
        self.port = port
        self.state = state
        self._server: asyncio.AbstractServer | None = None
        # E2E 测试钩子：POST /test_audio → 通知 BridgeServer 触发 sidecar 注入测试音频
        self._on_test_audio = on_test_audio or (lambda: False)
        self._test_audio_enabled = test_audio_enabled

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        logger.info("health server listening on %s:%s", self.host, self.port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            request_line = line.decode("utf-8", errors="replace").strip()
            parts = request_line.split(" ")
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"
            if path == "/health":
                body = json.dumps({
                    "status": "ok",
                    "sidecar_connected": bool(self.state.get("sidecar_connected")),
                    "rooms": 1 if self.state.get("room_id") else 0,
                }, ensure_ascii=False)
            elif path == "/metrics":
                body = json.dumps(self._metrics(), ensure_ascii=False)
            elif (path == "/test_audio" and method == "POST"
                  and self._test_audio_enabled):
                # E2E 测试端点：触发 sidecar 向手机端注入测试音频（验证下行播放链路）
                ok = await self._on_test_audio()
                body = json.dumps({"status": "ok" if ok else "sidecar_not_connected"}, ensure_ascii=False)
            else:
                await self._respond(writer, 404, "application/json", json.dumps({"error": "Not Found"}))
                return
            await self._respond(writer, 200, "application/json", body)
        except Exception as e:  # noqa: BLE001
            logger.debug("health handle error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    def _metrics(self) -> dict:
        m = dict(self.state)
        # 去掉不可序列化的会话对象引用（仅用于读实时指标）
        session = m.pop("_session_ref", None)
        if session is not None:
            m["up_frames"] = session.stats["up_frames"]
            m["down_frames"] = session.stats["down_frames"]
            m["apm_session_state"] = session.stats["apm_session_state"]
            m["last_peer_ts"] = session.stats["last_peer_ts"]
            m["up_queue_depth"] = session.stats.get("up_queue_depth", 0)
            m["down_queue_depth"] = session.stats.get("down_queue_depth", 0)
            m["queue_high_watermark"] = session.stats.get("queue_high_watermark", 0)
            m["queue_drops"] = session.stats.get("queue_drops", 0)
            m["backpressure_events"] = session.stats.get("backpressure_events", 0)
        else:
            m.setdefault("up_frames", 0)
            m.setdefault("down_frames", 0)
            m.setdefault("apm_session_state", "idle")
            m.setdefault("last_peer_ts", 0)
            m.setdefault("up_queue_depth", 0)
            m.setdefault("down_queue_depth", 0)
            m.setdefault("queue_high_watermark", 0)
            m.setdefault("queue_drops", 0)
            m.setdefault("backpressure_events", 0)
        m["rooms"] = 1 if m.get("room_id") else 0
        return m

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, ctype: str, body: str) -> None:
        reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
        data = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + data)
        await writer.drain()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
