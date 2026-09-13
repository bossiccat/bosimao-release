"""Qwen-Audio-3.0-Realtime bridge for the voice front-end layer."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
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


_PCM16K_TAPS = 31
_pcm16k_history = np.zeros(0, dtype=np.float64)


def _lowpass_taps(cutoff_hz: float, rate_hz: float, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2.0
    wc = 2.0 * np.pi * cutoff_hz / rate_hz
    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.where(n == 0, wc / np.pi, np.sin(wc * n) / (np.pi * n))
    h *= 0.54 - 0.46 * np.cos(2.0 * np.pi * np.arange(taps) / (taps - 1))  # Hamming
    return h / h.sum()                                                     # 直流增益=1


# 24k→16k：截止取目标奈奎斯特的 0.9 倍（16k → 7.2kHz），留过渡带
_PCM16K_TAPS_ARR = _lowpass_taps(0.45 * 16000, 24000, _PCM16K_TAPS)


def pcm24k_to_pcm16k(data: bytes) -> bytes:
    """24 kHz → 16 kHz，**带抗混叠低通**（2026-09-12 音质根因修复）。

    原实现是朴素抽取，无低通：

        idx = (np.arange(len(samples) * 2 // 3) * 1.5).astype(np.int64)

    24k→16k 会把 8–12 kHz 折叠回 0–8 kHz 变成非谐波噪声。这条路径是**模型回复音频的
    入口**（`event["delta"]`），所以听感上「问句干净、回复发毛」—— 问句由 edge-tts 直接
    合成 16k，从不经过这里；回复必经此处。实测回复谱亮度比问句高 68%，音调却正常，
    正是「高频噪声叠加在语音上」的指纹。

    实现要点：**跨调用有状态**（保留 N−1 个历史样本）。无状态滤波会让每块首尾失真，
    在流式增量（变长 delta）下形成新的周期性瑕疵 —— 等于用一个缺陷换另一个。
    """
    global _pcm16k_history
    samples = np.frombuffer(data, dtype=np.int16)
    if not len(samples):
        return b""
    buf = np.concatenate([_pcm16k_history, samples.astype(np.float64)])
    filt = np.convolve(buf, _PCM16K_TAPS_ARR, mode="valid")   # len == len(samples)
    n_out = len(samples) * 2 // 3
    if n_out <= 0:
        _pcm16k_history = buf[-(_PCM16K_TAPS - 1):]
        return b""
    idx = np.arange(n_out) * 1.5
    i0 = np.floor(idx).astype(np.int64)
    i0 = np.clip(i0, 0, len(filt) - 1)
    i1 = np.clip(i0 + 1, 0, len(filt) - 1)
    frac = idx - np.floor(idx)
    out = filt[i0] * (1.0 - frac) + filt[i1] * frac
    _pcm16k_history = buf[-(_PCM16K_TAPS - 1):]
    return np.clip(np.round(out), -32768, 32767).astype(np.int16).tobytes()


QWEN_RECONNECT_MAX_ATTEMPTS = 5


class QwenRealtimeBridge:
    def __init__(self, on_audio_out: Callable[[bytes], Awaitable[None]], on_text: Callable[[str], Awaitable[None]] | None = None, on_tool_call: Callable[[str, dict, str], Awaitable[str]] | None = None, api_url: str = "", token: str = "", system_prompt: str = "", on_error: Callable[[str], Awaitable[None]] | None = None, send_queue_frames: int = 25, on_user_speech: Callable[[], Awaitable[None]] | None = None) -> None:
        self._on_audio_out = on_audio_out
        self._on_text = on_text
        self._on_tool_call = on_tool_call
        # 服务端 VAD 判定用户开口（speech_started 事件）→ 会话层执行打断冲刷
        # （M2 决策归位：打断判定用云端声学+语义，不再依赖本地被回声污染的能量阈值）
        self._on_user_speech = on_user_speech
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
        # P0-5/F1：每个 reply 的 TTFB 观测锚点
        self._reply_t0 = 0.0
        self._first_audio_logged = False
        # 本 reply 模型侧产出的音频总字节（24k mono s16）。手机侧只能统计"到达了多少帧"，
        # 拿它与模型侧总量一比，才能把「模型本来就说得短」与「我们的管道丢了音频」分开。
        self._reply_bytes = 0
        # 上行解耦（2026-09-07）：feed_pcm 不再逐帧 await 云端 send——慢网会把
        # 20ms 上行消费循环整体拖死 → 队列积压 → 超 1000ms 帧龄整批判过期 = 吞话。
        # 改为有界队列 + 独立 sender task：feed 入队即返回；满则丢旧保新；
        # 25 帧 ≈ 500ms 缓冲，与上行帧龄上限同量级。
        self._send_q: asyncio.Queue = asyncio.Queue(maxsize=max(1, send_queue_frames))
        self._sender_task: asyncio.Task | None = None

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
        self._sender_task = asyncio.create_task(self._sender_loop())

    async def feed_pcm(self, pcm: bytes) -> None:
        if self._closed or self._dead:
            return
        if not self._started:
            await self.start()
        if self._ws is None:
            # 断线窗口（退避重连中）：入口即丢——入队只会占槽位，重连后帧已过时。
            # 丢弃并计数，节流日志（≥2s 一条），不向 20ms 高频调用方抛异常。
            self.dropped_frames += 1
            now = time.time()
            if now - self._last_drop_log >= 2.0:
                self._last_drop_log = now
                logger.warning("qwen link down, drop uplink frame #%d", self.dropped_frames)
            return
        # 有界入队即返回：慢网时 sender 消化慢，在此丢旧保新（绝不阻塞调用方）
        try:
            self._send_q.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self._send_q.get_nowait()   # 丢最旧
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
            self._send_q.put_nowait(pcm)

    async def cancel_response(self) -> bool:
        """主动取消正在进行的 response（**打断**用）。

        为什么必须显式发：`session.py` 原有的注释假定「云端 smart_turn 会自己
        response.cancel 并停发音频」，但实测**否掉了这个假设** —— 用户插话后旧 response
        仍继续下发到自然结束（实测 +3.18s），而两条打断路径（本地能量 / 云端 VAD）
        都只清我们这一侧、从未告诉模型停下。导致打断延迟恒在 1.5–1.8s。

        协议：OpenAI Realtime 兼容事件 `response.cancel`（Qwen 同族）。

        fail-soft：连接不在/已关闭时返回 False 并记日志，**不抛**——打断路径上抛异常
        会污染音频回调。
        """
        payload = {"type": "response.cancel"}
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("response.cancel 发送失败: %s", type(exc).__name__)
            return False

    async def _sender_loop(self) -> None:
        while not self._closed and not self._dead:
            pcm = await self._send_q.get()
            ws = self._ws
            if ws is None:
                # 断线窗口：积压帧已过时（重连后会话上下文已变），丢弃不补发
                self.dropped_frames += 1
                continue
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))
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
        # P0-5/F1：qwen 事件全打点（[lat] 前缀，mono 时间锚）——拆云端 TTFB
        if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                    "input_audio_buffer.committed", "response.created", "response.done", "error"}:
            logger.info("[lat] qwen event: %s %s mono=%.3f",
                        kind, str(event.get("error", ""))[:200], time.monotonic())
        if kind == "input_audio_buffer.speech_started" and self._on_user_speech:
            # 服务端确认的用户开口（smart_turn 声学+语义判定）——非语义声音
            # （回声瞬态/语气词）不会触发；这是可信赖的打断信号源。
            await self._invoke(self._on_user_speech)
        if kind == "response.created":
            self._reply_t0 = time.monotonic()
            self._first_audio_logged = False
            self._reply_bytes = 0
        if kind == "response.audio.delta" and event.get("delta") and not self._first_audio_logged:
            # P0-5/F1：首个 response.audio.delta——response.created 到首音频的云端 TTFB
            self._first_audio_logged = True
            ttfb_ms = int((time.monotonic() - self._reply_t0) * 1000) if self._reply_t0 else -1
            logger.info("[lat] first_audio_delta ttfb_ms=%d mono=%.3f", ttfb_ms, time.monotonic())
        if kind == "response.audio.delta" and event.get("delta"):
            raw = base64.b64decode(event["delta"])
            self._reply_bytes += len(raw)
            await self._invoke(self._on_audio_out, pcm24k_to_pcm16k(raw))
        if kind == "response.done":
            # 模型侧音频总量真值。口径：24k mono s16 ⇒ 秒 = 字节 / (24000 × 2)。
            # 判读：与手机侧「语音帧数 × 20ms」比 —— 两者接近 ⇒ 我们没丢；
            #       模型侧明显更短 ⇒ 模型本来就说得快/说得短。
            logger.info(
                "[lat] model audio done total_bytes=%d seconds=%.3f mono=%.3f",
                self._reply_bytes, self._reply_bytes / 48000.0, time.monotonic(),
            )
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
                logger.info("[lat] tool_call received name=%s call_id=%s mono=%.3f",
                            name, call_id, time.monotonic())
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
            # P0-5/F2：tool 生命周期打点（量出 tool 执行时长）
            logger.info("[lat] tool_done call_id=%s dur_ms=%d",
                        call_id, int((time.monotonic() - started) * 1000))
            ws = self._ws
            if ws is None:
                continue  # 断线窗口：新会话建立后云端会重新编排
            try:
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": output}}))
                await ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio", "text"]}}))
                # P0-5/F2：二轮 response.create 发出时刻
                logger.info("[lat] tool_output_sent call_id=%s mono=%.3f", call_id, time.monotonic())
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
        if self._sender_task:
            self._sender_task.cancel()
        if self._ws:
            await self._ws.close()
        self._ws = None


# Qwen 握手竞态（2026-09-07 真机实锤 92s 空洞）：session.created 是服务器应用
# session.update **之前**的默认配置宣告；session.updated 才代表 smart_turn VAD
# 已生效。旧实现收到 created 即返回，从未确认配置生效就开始灌音频。
QWEN_CONFIG_CONFIRM_TIMEOUT_S = 10.0


def default_turn_detection() -> dict:
    """turn_detection 配置（env 可调，零重编译 A/B）：
    - QWEN_TURN_DETECTION=smart_turn（默认）| server_vad
      smart_turn：声学+语义双重轮次检测，非语义声音（嗯/啊/回声瞬态）不触发
      打断——决策归位（M2）的判定引擎。server_vad：纯声学，参数可调。
    - QWEN_SILENCE_DURATION_MS（仅 server_vad 生效，200-6000，默认 800）：
      静音判定时长；run2 实测中文长句需 ≥1000 才不在自然停顿处断句。
    """
    kind = (os.environ.get("QWEN_TURN_DETECTION") or "smart_turn").strip().lower()
    if kind not in {"smart_turn", "server_vad"}:
        kind = "smart_turn"
    detection: dict = {"type": kind}
    if kind == "server_vad":
        try:
            silence = int(os.environ.get("QWEN_SILENCE_DURATION_MS", "800"))
        except ValueError:
            silence = 800
        detection["silence_duration_ms"] = max(200, min(6000, silence))
    return detection


async def connect_qwen(api_url: str, token: str, system_prompt: str, tools: list[dict], config_confirm_timeout_s: float = QWEN_CONFIG_CONFIRM_TIMEOUT_S) -> tuple[Any, str]:
    ws = await connect_ws(api_url, token)
    await ws.send(json.dumps({"type": "session.update", "session": {"modalities": ["audio", "text"], "instructions": system_prompt, "input_audio_format": "pcm", "output_audio_format": "pcm", "max_history_turns": 50, "tools": tools, "turn_detection": default_turn_detection()}}))
    deadline = time.monotonic() + config_confirm_timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # fail-open：保持旧可用性（不挂死握手），但必须 ERROR 露出，不再静默
            logger.error(
                "Qwen session config NOT confirmed within %.1fs (no session.updated); "
                "continuing on DEFAULT server config — smart_turn VAD may be inactive, "
                "expect speech never committed", config_confirm_timeout_s,
            )
            return ws, ""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            continue  # 回到循环顶检查 deadline → fail-open
        msg = json.loads(raw)
        mtype = msg.get("type")
        if mtype == "session.updated":
            session = msg.get("session", {})
            sid = msg.get("session_id", session.get("id", ""))
            logger.info("Qwen session config confirmed (session.updated) sid=%s", sid)
            return ws, sid
        # session.created 与其他前置事件：只消费，不作为就绪依据
        if mtype == "error":
            raise RuntimeError(f"Qwen session failed: {msg}")
