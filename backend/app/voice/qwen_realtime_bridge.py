"""Qwen-Audio-3.0-Realtime bridge for the voice front-end layer."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

import numpy as np

from .apm_handshake import connect_ws

logger = logging.getLogger(__name__)

QWEN_COORDINATION_TOOLS = [
    {"type": "function", "function": {"name": "spawn_agent_thread", "description": "Create a persistent background worker for a complex task.", "parameters": {"type": "object", "properties": {"user_speech": {"type": "string"}}, "required": ["user_speech"]}}},
    {"type": "function", "function": {"name": "agent_status", "description": "Read concise status of a background worker.", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]}}},
    {"type": "function", "function": {"name": "steer_agent_thread", "description": "Redirect or stop a running background worker.", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}, "instruction": {"type": "string"}, "action": {"type": "string", "enum": ["steer", "cancel"]}}, "required": ["thread_id", "action"]}}},
    {"type": "function", "function": {"name": "approve_reply", "description": "Ask the user to approve a risky local operation.", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}, "approval_id": {"type": "string"}, "summary": {"type": "string"}}, "required": ["thread_id", "approval_id", "summary"]}}},
]


def pcm24k_to_pcm16k(data: bytes) -> bytes:
    samples = np.frombuffer(data, dtype=np.int16)
    if not len(samples):
        return b""
    idx = (np.arange(len(samples) * 2 // 3) * 1.5).astype(np.int64)
    return samples[idx[idx < len(samples)]].tobytes()


class QwenRealtimeBridge:
    def __init__(self, on_audio_out: Callable[[bytes], Awaitable[None]], on_text: Callable[[str], Awaitable[None]] | None = None, on_tool_call: Callable[[str, dict, str], Awaitable[str]] | None = None, api_url: str = "", token: str = "", system_prompt: str = "") -> None:
        self._on_audio_out = on_audio_out
        self._on_text = on_text
        self._on_tool_call = on_tool_call
        self._api_url = api_url
        self._token = token
        self._system_prompt = system_prompt
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._started = False
        self.session_id = ""

    async def start(self) -> None:
        if self._started or self._closed:
            return
        self._ws, self.session_id = await connect_qwen(self._api_url, self._token, self._system_prompt, QWEN_COORDINATION_TOOLS)
        self._started = True
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def feed_pcm(self, pcm: bytes) -> None:
        if self._closed:
            return
        if not self._started:
            await self.start()
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))

    async def _recv_loop(self) -> None:
        while not self._closed and self._ws is not None:
            try:
                event = json.loads(await self._ws.recv())
                await self._handle_event(event)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("qwen recv loop ended: %s", exc)
                return

    async def _invoke(self, callback, *args) -> None:
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("type", "")
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
            if self._on_tool_call:
                output = await self._on_tool_call(name, args, call_id)
                await self._ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": output}}))
                await self._ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio", "text"]}}))

    async def close(self) -> None:
        self._closed = True
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
