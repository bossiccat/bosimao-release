"""BridgeServer —— localhost WS 服务端（127.0.0.1:19092，sidecar 是客户端）

- 首个消息必须为完整当前会话 hello；随后 up_audio / peer_state 分发到 PeerVoiceSession
- 会话下行（down_audio / ctrl）经 _send_msg 回调写到当前 WS
- MVP 单用户：新 sidecar 连接顶替旧连接（旧连接 close）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from .drain_ack import (
    DrainAcknowledger, TerminationRegistry, build_ack_reporter,
    parse_note_termination,
)
from .redemption import HelloRedemptionClient, HelloRedemptionError, validate_hello
from .session import PeerVoiceSession

logger = logging.getLogger(__name__)


class BridgeServer:
    """sidecar ↔ rtc_bridge 桥接服务端"""

    def __init__(self, cfg, state: dict, *, redemption=None,
                 ack_reporter=None) -> None:
        self.cfg = cfg
        self.state = state                       # 指标/健康共享字典（health.py 读取）
        self._redemption = redemption or HelloRedemptionClient(
            base_url=cfg.control_plane_base_url,
            service_credential=cfg.control_plane_service_credential,
            ca_file=cfg.control_plane_ca_file,
            client_cert_file=cfg.control_plane_client_cert_file,
            client_key_file=cfg.control_plane_client_key_file,
            gateway_assertion=cfg.control_plane_gateway_assertion,
            connect_timeout_s=cfg.control_plane_connect_timeout_s,
            total_timeout_s=cfg.control_plane_total_timeout_s,
        )
        self._ack_reporter = build_ack_reporter(cfg, ack_reporter)
        # 终止上下文注册表：sidecar 经 WS ctrl note_termination 中继注入；
        # 无注入 → drain 时不上报（跳过，不硬编码）。
        self._terminations = TerminationRegistry()
        self._drain_ack: DrainAcknowledger | None = None
        self._ws: Any = None
        self._session: PeerVoiceSession | None = None
        self._session_id = ""
        self._send_lock = asyncio.Lock()
        self._activation_lock = asyncio.Lock()

    @property
    def sidecar_connected(self) -> bool:
        return self._ws is not None and self._session is not None

    def note_termination(self, session_id: str, termination_id: str) -> None:
        """进程内注入终止上下文（与 WS ctrl 中继等价的接缝，测试/未来接线用）。"""
        self._terminations.note(session_id, termination_id)

    async def _send(self, msg: dict) -> None:
        """向当前 sidecar 发送 JSON（带锁；连接断开时静默失败）"""
        await self._send_to(self._ws, msg)

    async def _send_to(self, ws, msg: dict) -> None:
        if ws is None:
            raise ConnectionError("sidecar 未连接")
        async with self._send_lock:
            await ws.send(json.dumps(msg, ensure_ascii=False))

    async def handler(self, ws) -> None:
        logger.info("sidecar ws connected %s", ws.remote_address)

        try:
            # 首帧 hello
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw)
            try:
                hello = validate_hello(hello)
            except HelloRedemptionError:
                await self._send_to(
                    ws, {"type": "ctrl", "action": "exit", "reason": "invalid_session_hello"}
                )
                return
            try:
                await self._redemption.redeem(hello)
            except Exception:  # noqa: BLE001 - any redemption failure is fail-closed
                logger.warning("sidecar hello redemption rejected")
                await self._send_to(
                    ws, {"type": "ctrl", "action": "exit", "reason": "hello_redemption_failed"}
                )
                return
            session_id = hello["session_id"]
            device_id = hello["device_id"]
            room_id = hello["room_id"]
            sdk_version = hello.get("sdk_version", "")

            # 终止上下文上报器先于会话创建（APM 取消回调需引用）
            drain_ack = DrainAcknowledger(
                session_id=session_id, device_id=device_id, room_id=room_id,
                generation=hello["generation"], reporter=self._ack_reporter,
                terminations=self._terminations,
            )

            candidate = PeerVoiceSession(
                device_id=device_id,
                room_id=room_id,
                send_msg=lambda msg: self._send_to(ws, msg),
                apm_api_url=self.cfg.apm_api_url,
                apm_system_prompt=self.cfg.apm_system_prompt,
                apm_token=self.cfg.apm_token,
                down_frame_ms=self.cfg.down_frame_ms,
                sample_rate=self.cfg.sample_rate,
                up_max_frames=self.cfg.up_max_frames,
                up_max_bytes=self.cfg.up_max_bytes,
                up_max_frame_age_ms=self.cfg.up_max_frame_age_ms,
                down_max_frames=self.cfg.down_max_frames,
                down_max_bytes=self.cfg.down_max_bytes,
                down_max_frame_age_ms=self.cfg.down_max_frame_age_ms,
                on_apm_cancelled=lambda clean: drain_ack.report_apm_cancel(
                    closed_cleanly=clean
                ),
            )
            try:
                await candidate.start()
            except Exception:
                try:
                    await candidate.close()
                except Exception:  # noqa: BLE001
                    logger.debug("candidate session cleanup failed", exc_info=True)
                raise
            async with self._activation_lock:
                old = self._ws
                old_session = self._session
                old_drain_ack = self._drain_ack
                self._ws = ws
                self._session = candidate
                self._drain_ack = drain_ack
                self._session_id = session_id
                self.state["sidecar_sdk_version"] = sdk_version
                self.state["room_id"] = room_id
                self.state["device_id"] = device_id
                self.state["sidecar_connected"] = True
                self.state["_session_ref"] = candidate   # health /metrics 读取实时指标
            if old is not None and old is not ws:
                try:
                    await old.close(code=1000, reason="replaced")
                except Exception:  # noqa: BLE001
                    logger.debug("best-effort bridge cleanup failed", exc_info=True)
            if old_session is not None and old_session is not candidate:
                try:
                    await old_session.close()
                except Exception:  # noqa: BLE001
                    logger.debug("best-effort bridge cleanup failed", exc_info=True)
                # 旧会话被顶替 = 它的 drain 已发生 → 补一次上报（fire-once 幂等）
                if old_drain_ack is not None:
                    try:
                        await old_drain_ack.report_drain(closed_cleanly=True)
                    except Exception:  # noqa: BLE001
                        logger.debug("replaced drain ack failed", exc_info=True)
            await self._send_to(ws, {"type": "ready"})

            # 接收循环
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        except asyncio.TimeoutError:
            logger.warning("sidecar hello 超时，关闭连接")
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("sidecar ws closed: %s", e.code)
        except Exception as e:  # noqa: BLE001
            logger.warning("sidecar handler error: %s", e)
        finally:
            await self._cleanup(ws)

    async def _dispatch(self, msg: dict) -> None:
        mtype = msg.get("type")
        session = self._session
        if session is None:
            return
        if mtype == "up_audio" and msg.get("pcm_b64"):
            try:
                import base64

                pcm = base64.b64decode(msg["pcm_b64"])
                await session.on_up_audio(pcm)
            except Exception as e:  # noqa: BLE001
                logger.warning("up_audio decode failed: %s", e)
        elif mtype == "peer_state":
            state = msg.get("state")
            user_id = msg.get("user_id", "")
            if state == "enter":
                await session.on_peer_enter(user_id)
            elif state == "leave":
                await session.on_peer_leave(user_id)
        elif mtype == "ctrl":
            self._handle_sidecar_ctrl(msg)
        else:
            logger.debug("ignored sidecar msg type=%s", mtype)

    def _handle_sidecar_ctrl(self, msg: dict) -> None:
        """sidecar 上行 ctrl：note_termination 终止上下文中继（fail-safe）。

        仅接受与当前活动会话匹配的注入；畸形/不匹配只记 debug，不影响主链路。
        """
        parsed = parse_note_termination(msg)
        if parsed is None:
            logger.debug("ignored sidecar ctrl type=%s action=%s",
                         msg.get("type"), msg.get("action"))
            return
        session_id, termination_id = parsed
        if session_id != self._session_id or self._session is None:
            logger.debug("note_termination session mismatch sid=%s", session_id)
            return
        self._terminations.note(session_id, termination_id)

    async def _cleanup(self, ws) -> None:
        # 身份检查：仅当 self._ws 仍指向本 handler 的连接时才清理。
        # 否则旧连接被顶替后的清理会误伤新连接（旧 handler 的 finally 关掉新 session，
        # 新连接"活着"但消息全丢——高压测试 S6 实锤的顶替竞态）。
        if self._ws is not ws:
            return
        self.state["sidecar_connected"] = False
        self.state["_session_ref"] = None
        self.state["room_id"] = ""
        self.state["device_id"] = ""
        closed_cleanly = True
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                closed_cleanly = False
                logger.debug("session close failed during drain", exc_info=True)
            self._session = None
        drain_ack = self._drain_ack
        self._drain_ack = None
        self._ws = None
        self._session_id = ""
        if drain_ack is not None:
            try:
                await drain_ack.report_drain(
                    closed_cleanly=closed_cleanly,
                    error_code=None if closed_cleanly
                    else "bridge_session_close_failed",
                )
            except Exception:  # noqa: BLE001 - 上报绝不影响清理与主链路
                logger.debug("drain acknowledgement failed", exc_info=True)

    async def terminate_device(self, device_id: str,
                               session_ids: list[str]) -> list[str]:
        """Close the matching live sidecar session and return confirmed session ids."""
        ws = self._ws
        session = self._session
        if ws is None or session is None or session.device_id != device_id:
            return []
        if self._session_id not in session_ids:
            return []
        session_id = self._session_id
        try:
            await self._send({"type": "ctrl", "action": "exit", "reason": "device_revoked"})
        except Exception:  # noqa: BLE001
            logger.debug("device revoke notification failed", exc_info=True)
        await ws.close(code=1008, reason="device revoked")
        await self._cleanup(ws)
        return [session_id]

    async def send_ctrl_exit(self, reason: str) -> None:
        """后端控制：通知 sidecar 退房（会话结束）"""
        if self._ws is not None:
            try:
                await self._send({"type": "ctrl", "action": "exit", "reason": reason})
            except Exception:  # noqa: BLE001
                logger.warning("send ctrl exit failed: %s", reason)

    async def send_test_audio(self) -> bool:
        """E2E 测试：通知 sidecar 向手机端注入 2s 测试音频（验证下行播放链路）。
        返回是否已发送给在线 sidecar。"""
        if self._ws is None:
            logger.warning("test_audio ignored: sidecar not connected")
            return False
        try:
            await self._send({"type": "ctrl", "action": "test_audio", "reason": "e2e"})
            logger.info("test_audio ctrl sent to sidecar")
            return True
        except Exception:  # noqa: BLE001
            logger.exception("test_audio send failed")
            return False
