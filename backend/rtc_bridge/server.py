"""BridgeServer —— localhost WS 服务端（127.0.0.1:19092，sidecar 是客户端）

- 首个消息必须为完整当前会话 hello；随后 up_audio / peer_state 分发到 PeerVoiceSession
- 会话下行（down_audio / ctrl）经 _send_msg 回调写到当前 WS
- MVP 单用户：新 sidecar 连接顶替旧连接（旧连接 close）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from .session import PeerVoiceSession
from app.brain.agent_thread_registry import AgentThreadRegistry
from app.voice.qwen_realtime_bridge import QwenRealtimeBridge
from app.brain.hermes_worker_runner import HermesWorkerRunner

logger = logging.getLogger(__name__)


class BridgeServer:
    """sidecar ↔ rtc_bridge 桥接服务端"""

    def __init__(
        self,
        cfg,
        state: dict,
        on_voice_intent: Callable[[str], Awaitable[None]] | None = None,
        thread_registry: AgentThreadRegistry | None = None,
        worker_runner: HermesWorkerRunner | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state                       # 指标/健康共享字典（health.py 读取）
        self._ws: Any = None
        self._session: PeerVoiceSession | None = None
        self._session_id = ""
        self._send_lock = asyncio.Lock()
        self._on_voice_intent = on_voice_intent   # AI 文本 → Brain 路由回调（可选）
        self._thread_registry = thread_registry or AgentThreadRegistry()
        # env 驱动装配（canary/feature-off 旋钮），flags 写入 state 供 health 观测。
        self._worker_runner = worker_runner or HermesWorkerRunner.from_env(self._thread_registry)
        flags_fn = getattr(self._worker_runner, "feature_flags", None)
        if flags_fn is not None:
            state.setdefault("worker_feature_flags", flags_fn())
        self._worker_tasks: dict[str, asyncio.Task] = {}
        self._command_consumer: asyncio.Task | None = None

    async def start_command_consumer(self, interval: float = 0.2) -> None:
        """Start durable approval command polling; safe to call after restart."""
        if self._command_consumer is None:
            self._thread_registry.recover_commands()
            self._command_consumer = asyncio.create_task(self._consume_commands(interval))

    async def stop_command_consumer(self) -> None:
        task = self._command_consumer
        self._command_consumer = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _schedule_claimed_command(self, command: dict) -> None:
        """Gate worker launch on an atomic queued-to-running thread claim."""
        thread_id = command["thread_id"]
        if self._thread_registry.claim_queued(thread_id) is None:
            self._thread_registry.complete_command(command["command_id"])
            return
        if thread_id in self._worker_tasks:
            self._thread_registry.complete_command(command["command_id"])
            return
        self._worker_tasks[thread_id] = asyncio.create_task(
            self._run_worker(thread_id, command["command_id"]),
            name=f"hermes-worker-{thread_id}",
        )

    async def _consume_commands(self, interval: float) -> None:
        while True:
            try:
                for command in self._thread_registry.claim_commands():
                    if command.get("command") == "start_worker":
                        self._schedule_claimed_command(command)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("approval command consumer failed")
                await asyncio.sleep(interval)

    @property
    def thread_registry(self) -> AgentThreadRegistry:
        return self._thread_registry

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
                voice_engine=self.cfg.voice_engine,
                qwen_api_url=self.cfg.qwen_api_url,
                qwen_token=self.cfg.qwen_token,
                qwen_system_prompt=self.cfg.qwen_system_prompt,
                on_agent_tool=lambda name, args, call_id: self._handle_agent_tool(name, args, call_id),
                down_frame_ms=self.cfg.down_frame_ms,
                sample_rate=self.cfg.sample_rate,
                up_max_frames=self.cfg.up_max_frames,
                up_max_bytes=self.cfg.up_max_bytes,
                up_max_frame_age_ms=self.cfg.up_max_frame_age_ms,
                down_max_frames=self.cfg.down_max_frames,
                down_max_bytes=self.cfg.down_max_bytes,
                down_max_frame_age_ms=self.cfg.down_max_frame_age_ms,
                on_voice_intent=self._on_voice_intent,
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

    async def _handle_agent_tool(self, name: str, args: dict, call_id: str) -> str:
        del call_id
        if name == "spawn_agent_thread":
            result = self._thread_registry.handle_tool(name, args)
            return json.dumps(result, ensure_ascii=False)
        if name == "steer_agent_thread" and str(args.get("action", "steer")) == "cancel":
            thread_id = str(args.get("thread_id", ""))
            cancelled = await self._worker_runner.cancel(thread_id)
            result = self._thread_registry.get(thread_id)
            if result is None:
                result = {"error": "thread_not_found"}
            elif not cancelled and result["status"] != "cancelled":
                result = self._thread_registry.steer(thread_id, action="cancel") or result
            return json.dumps(result, ensure_ascii=False)
        result = self._thread_registry.handle_tool(name, args)
        return json.dumps(result, ensure_ascii=False)

    async def approve_agent_thread(self, thread_id: str, approval_id: str) -> dict:
        result = self._thread_registry.approve(thread_id, approval_id)
        if result.get("status") != "queued":
            return result
        for command in self._thread_registry.claim_commands():
            if command.get("command") == "start_worker":
                self._schedule_claimed_command(command)
        return result

    async def _run_worker(self, thread_id: str, command_id: str | None = None) -> None:
        try:
            await self._worker_runner.start(thread_id)
        finally:
            self._worker_tasks.pop(thread_id, None)
            if command_id:
                self._thread_registry.complete_command(command_id)

    async def wait_for_worker(self, thread_id: str) -> None:
        task = self._worker_tasks.get(thread_id)
        if task is not None:
            await task

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
