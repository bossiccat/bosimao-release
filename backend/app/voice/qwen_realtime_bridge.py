"""Qwen-Audio-3.0-Realtime bridge for the voice front-end layer."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable

import numpy as np

from .apm_handshake import connect_ws
from .apm_reconnect import ReconnectScheduler

logger = logging.getLogger(__name__)

QWEN_COORDINATION_TOOLS = [
    {"type": "function", "function": {"name": "spawn_agent_thread", "description": "Create a persistent background worker for a complex task.", "parameters": {"type": "object", "properties": {"user_speech": {"type": "string"}}, "required": ["user_speech"]}}},
    {"type": "function", "function": {"name": "agent_status", "description": "Read concise status of a background worker.", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]}}},
    {"type": "function", "function": {"name": "steer_agent_thread", "description": "Redirect or stop a running background worker.", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}, "instruction": {"type": "string"}, "action": {"type": "string", "enum": ["steer", "cancel"]}}, "required": ["thread_id", "action"]}}},
    {"type": "function", "function": {"name": "approve_reply", "description": "Ask the user to approve a risky local operation. The server generates the approval identifier.", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}, "summary": {"type": "string"}}, "required": ["thread_id", "summary"]}}},
]


def pcm24k_to_pcm16k(data: bytes) -> bytes:
    samples = np.frombuffer(data, dtype=np.int16)
    if not len(samples):
        return b""
    idx = (np.arange(len(samples) * 2 // 3) * 1.5).astype(np.int64)
    return samples[idx[idx < len(samples)]].tobytes()


QWEN_RECONNECT_MAX_ATTEMPTS = 5


class QwenRealtimeBridge:
    def __init__(self, on_audio_out: Callable[[bytes], Awaitable[None]], on_text: Callable[[str], Awaitable[None]] | None = None, on_tool_call: Callable[[str, dict, str], Awaitable[str]] | None = None, api_url: str = "", token: str = "", system_prompt: str = "", on_error: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._on_audio_out = on_audio_out
        self._on_text = on_text
        self._on_tool_call = on_tool_call
        self._api_url = api_url
        self._token = token
        self._system_prompt = system_prompt
        self._on_error = on_error         # 重连放弃后上报（手机端可感知，不静默）
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._started = False
        self._dead = False                # 重连放弃后的终态：须上层重建实例恢复
        self._reconnect_lock = asyncio.Lock()
        self._last_drop_log = 0.0         # 断线窗口丢帧日志节流（≥2s 一条）
        self._last_error = ""             # 最近一次链路错误（idle 超时/1007 等）
        self.session_id = ""
        self.reconnects = 0               # 成功重连次数（观测）
        self.dropped_frames = 0           # 断线窗口丢弃的上行帧计数
        # P0（2026-09-06 真机实锤）：recv 循环异常退出后旧实现零重连，
        # 云端 180s idle 超时即永久静音 → 指数退避自动重连（1s/2s/.../60s 封顶）
        self._scheduler = ReconnectScheduler(
            attempt=self._reconnect_attempt,
            on_give_up=self._on_reconnect_give_up,
            max_attempts=QWEN_RECONNECT_MAX_ATTEMPTS,
        )
        # P0-4：tool_call 去同步化——同步 await 会冻结 recv 循环（下行音频冻结），
        # tool 时长全额计入首音频延迟。单 worker 串行队列：不丢调用、天然免锁。
        self._tool_queue: asyncio.Queue = asyncio.Queue()
        self._tool_worker_task: asyncio.Task | None = None

    @property
    def dead(self) -> bool:
        """重连放弃后的终态：须由上层重建实例（peer enter 重建路径）恢复"""
        return self._dead

    async def start(self) -> None:
        if self._started or self._closed:
            return
        self._ws, self.session_id = await connect_qwen(self._api_url, self._token, self._system_prompt, QWEN_COORDINATION_TOOLS)
        self._started = True
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def feed_pcm(self, pcm: bytes) -> None:
        if self._closed or self._dead:
            return
        if not self._started:
            await self.start()
        if self._ws is None:
            # 断线窗口（退避重连中）：丢弃并计数，节流日志（≥2s 一条），不向 20ms 高频调用方抛异常
            self.dropped_frames += 1
            now = time.time()
            if now - self._last_drop_log >= 2.0:
                self._last_drop_log = now
                logger.warning("qwen link down, drop uplink frame #%d", self.dropped_frames)
            return
        try:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))
        except Exception as exc:  # noqa: BLE001
            if self._closed or self._dead:
                return
            # 发送失败 = 链路断：标记断链并交退避调度器（recv 循环也会感知退出）
            logger.warning("qwen uplink send failed (%s), scheduling reconnect", exc)
            self._last_error = str(exc)
            self._ws = None
            self.dropped_frames += 1
            self._scheduler.schedule()

    async def _recv_loop(self) -> None:
        # ws 作快照：循环期间 _ws 可能被发送路径置 None（同一断链事件）
        ws = self._ws
        while not self._closed and not self._dead and ws is not None:
            try:
                event = json.loads(await ws.recv())
                await self._handle_event(event)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if self._closed:
                    return
                self._last_error = str(exc)
                logger.warning("qwen recv loop ended: %s", exc)
                break
        # P0：recv 循环退出（连接断/服务端 1007 关链路）= 链路 down → 退避重连。
        # 仅当仍是本代链路才调度：若期间已重连成功（_ws 换代），跳过防误伤。
        if not self._closed and not self._dead and self._ws is ws:
            self._ws = None
            self._scheduler.schedule()

    async def _reconnect_attempt(self) -> bool:
        """scheduler 单次尝试：完整重握手（新 ws + 新 session_id，重带提示词/工具）"""
        async with self._reconnect_lock:
            if self._closed or self._dead:
                return True  # 已关闭/已放弃：让循环退出
            try:
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._ws = None
                if self._recv_task is not None:
                    self._recv_task.cancel()
                    try:
                        await self._recv_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    self._recv_task = None
                self._ws, self.session_id = await connect_qwen(self._api_url, self._token, self._system_prompt, QWEN_COORDINATION_TOOLS)
                self._recv_task = asyncio.create_task(self._recv_loop())
                self.reconnects += 1
                logger.info("qwen reconnected, session_id=%s", self.session_id)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("qwen reconnect attempt failed: %s", exc)
                self._last_error = str(exc)
                # 半开连接（握手中途失败）必须关闭，防泄漏
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._ws = None
                return False

    async def _on_reconnect_give_up(self) -> None:
        """连续失败达上限：进入终态并经 on_error 上报（手机端感知"云端引擎断开"）"""
        self._dead = True
        message = f"云端引擎断开：连续 {self._scheduler.max_attempts} 次重连失败"
        if self._last_error:
            message = f"{message}: {self._last_error}"
        if self._on_error is None:
            logger.error("qwen bridge gave up reconnecting: %s", message)
            return
        try:
            cb = self._on_error(message)
            if asyncio.iscoroutine(cb):
                asyncio.get_running_loop().create_task(cb)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_error callback failed: %s", e)

    async def _invoke(self, callback, *args) -> None:
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("type", "")
        # 关键事件日志：排查"千问无响应"时确认 VAD/提交/响应生命周期（2026-09-04）
        if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                    "input_audio_buffer.committed", "response.created", "error"}:
            logger.info("qwen event: %s %s", kind, str(event.get("error", ""))[:200])
        if kind == "response.audio.delta" and event.get("delta"):
            await self._invoke(self._on_audio_out, pcm24k_to_pcm16k(base64.b64decode(event["delta"])))
        elif kind in {"response.audio_transcript.delta", "response.text.delta"} and event.get("delta") and self._on_text:
            await self._invoke(self._on_text, event["delta"])
        elif kind in {"response.function_call_arguments.done", "response.function_call.done"}:
            name = event.get("name", "")
            args = event.get("arguments", "{}")
            if isinstance(args, str):
                args = json.loads(args or "{}")
            call_id = event.get("call_id", event.get("item_id", ""))
            if name == "approve_reply" and "approval_id" in args:
                args = {key: value for key, value in args.items() if key != "approval_id"}
            if self._on_tool_call:
                # P0-4：入队交专职 worker 串行执行，recv 循环立即返回继续处理事件
                self._tool_queue.put_nowait((name, args, call_id))
                self._ensure_tool_worker()

    def _ensure_tool_worker(self) -> None:
        if self._tool_worker_task is None or self._tool_worker_task.done():
            self._tool_worker_task = asyncio.create_task(self._tool_worker())

    async def _tool_worker(self) -> None:
        """串行执行工具调用：完成回调里发二轮 conversation.item.create + response.create"""
        while not self._closed and not self._dead:
            try:
                name, args, call_id = await self._tool_queue.get()
            except asyncio.CancelledError:
                return
            started = time.monotonic()
            try:
                output = await self._on_tool_call(name, args, call_id)
            except Exception as exc:  # noqa: BLE001 - 工具失败也要回填，防云端挂等
                logger.warning("tool_call failed call_id=%s: %s", call_id, exc)
                output = json.dumps({"error": str(exc)})
            logger.info("tool_done call_id=%s dur_ms=%d",
                        call_id, int((time.monotonic() - started) * 1000))
            ws = self._ws
            if ws is None:
                continue  # 断线窗口：新会话建立后云端会重新编排
            try:
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": output}}))
                await ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio", "text"]}}))
            except Exception as exc:  # noqa: BLE001
                if not self._closed and not self._dead:
                    logger.warning("qwen tool output send failed (%s), scheduling reconnect", exc)
                    self._last_error = str(exc)
                    self._ws = None
                    self._scheduler.schedule()

    async def close(self) -> None:
        self._closed = True
        self._scheduler.cancel()
        if self._tool_worker_task:
            self._tool_worker_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None


async def connect_qwen(api_url: str, token: str, system_prompt: str, tools: list[dict]) -> tuple[Any, str]:
    ws = await connect_ws(api_url, token)
    await ws.send(json.dumps({"type": "session.update", "session": {"modalities": ["audio", "text"], "instructions": system_prompt, "input_audio_format": "pcm", "output_audio_format": "pcm", "max_history_turns": 50, "tools": tools, "turn_detection": {"type": "smart_turn"}}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("type") in {"session.updated", "session.created"}:
            return ws, msg.get("session_id", msg.get("session", {}).get("id", ""))
        if msg.get("type") == "error":
            raise RuntimeError(f"Qwen session failed: {msg}")
