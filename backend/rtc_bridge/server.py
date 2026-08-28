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

from .drain_ack import (
    DrainAcknowledger, TerminationRegistry, build_ack_reporter,
    parse_note_termination,
)
from .redemption import HelloRedemptionClient, HelloRedemptionError, validate_hello
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
        *,
        redemption=None,
        ack_reporter=None,
    ) -> None:
        self.cfg = cfg
        self.state = state                       # 指标/健康共享字典（health.py 读取）
        # redemption 客户端惰性构建（首次 hello 兑付时）：配置缺失不得阻断进程启动——
        # 此时任何 hello 兑付都会失败，handler 走 fail-closed 拒绝会话（行为不变）。
        self._redemption_override = redemption
        self._redemption_client: HelloRedemptionClient | None = None
        self._ack_reporter = build_ack_reporter(cfg, ack_reporter)
        # 终止上下文注册表：sidecar 经 WS ctrl note_termination 中继注入；
        # 无注入 → drain 时不上报（跳过，不硬编码）。
        self._terminations = TerminationRegistry()
        self._drain_ack: DrainAcknowledger | None = None
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
        self._activation_lock = asyncio.Lock()

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
    def _redemption(self) -> HelloRedemptionClient:
        """hello 兑付客户端：显式注入优先；否则按 cfg 惰性构建（配置缺失即抛，fail-closed）"""
        if self._redemption_override is not None:
            return self._redemption_override
        if self._redemption_client is None:
            self._redemption_client = HelloRedemptionClient(
                base_url=self.cfg.control_plane_base_url,
                service_credential=self.cfg.control_plane_service_credential,
                ca_file=self.cfg.control_plane_ca_file,
                client_cert_file=self.cfg.control_plane_client_cert_file,
                client_key_file=self.cfg.control_plane_client_key_file,
                gateway_assertion=self.cfg.control_plane_gateway_assertion,
                connect_timeout_s=self.cfg.control_plane_connect_timeout_s,
                total_timeout_s=self.cfg.control_plane_total_timeout_s,
            )
        return self._redemption_client

    @_redemption.setter
    def _redemption(self, value) -> None:
        self._redemption_override = value

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
