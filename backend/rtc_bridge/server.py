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

from .session import PeerVoiceSession

logger = logging.getLogger(__name__)


class BridgeServer:
    """sidecar ↔ rtc_bridge 桥接服务端"""

    def __init__(self, cfg, state: dict) -> None:
        self.cfg = cfg
        self.state = state                       # 指标/健康共享字典（health.py 读取）
        self._ws: Any = None
        self._session: PeerVoiceSession | None = None
        self._session_id = ""
        self._send_lock = asyncio.Lock()

    @property
    def sidecar_connected(self) -> bool:
        return self._ws is not None and self._session is not None

    async def _send(self, msg: dict) -> None:
        """向当前 sidecar 发送 JSON（带锁；连接断开时静默失败）"""
        ws = self._ws
        if ws is None:
            raise ConnectionError("sidecar 未连接")
        async with self._send_lock:
            await ws.send(json.dumps(msg, ensure_ascii=False))

    async def handler(self, ws) -> None:
        # 顶替旧连接（MVP 单 sidecar）——必须先接管 self._ws 再 close 旧连接：
        # 否则 await old.close() 握手期间 self._ws 仍指向旧连接，旧 handler 的 finally
        # 清理会通过身份检查误伤新连接（压测 S6 实锤的顶替竞态窗口）。
        old = self._ws
        old_session = self._session
        self._ws = ws
        if old is not None and old is not ws:
            try:
                await old.close(code=1000, reason="replaced")
            except Exception:  # noqa: BLE001
                pass
        # 旧 session 显式释放（旧 handler 的 _cleanup 会因身份检查跳过，这里必须兜底，防泄漏）
        if old_session is not None:
            try:
                await old_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        logger.info("sidecar ws connected %s", ws.remote_address)

        try:
            # 首帧 hello
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                await self._send({"type": "ctrl", "action": "exit", "reason": "bad_hello"})
                return
            session_id = hello.get("session_id")
            device_id = hello.get("device_id")
            room_id = hello.get("room_id")
            if not all(
                isinstance(value, str) and bool(value.strip())
                for value in (session_id, device_id, room_id)
            ):
                await self._send(
                    {"type": "ctrl", "action": "exit", "reason": "invalid_session_hello"}
                )
                return
            sdk_version = hello.get("sdk_version", "")
            self.state["sidecar_sdk_version"] = sdk_version

            self._session = PeerVoiceSession(
                device_id=device_id,
                room_id=room_id,
                send_msg=self._send,
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
            )
            await self._session.start()
            self._session_id = session_id
            self.state["room_id"] = room_id
            self.state["device_id"] = device_id
            self.state["sidecar_connected"] = True
            self.state["_session_ref"] = self._session   # health /metrics 读取实时指标
            await self._send({"type": "ready"})

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
        else:
            logger.debug("ignored sidecar msg type=%s", mtype)

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
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._ws = None
        self._session_id = ""

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
            pass
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
