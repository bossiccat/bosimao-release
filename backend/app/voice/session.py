"""VoiceSession 管理（mobile-voice-spec §8.1 session.py）

- 连接注册/互斥：同一 device_id 新 hello 踢旧连接
- 心跳：服务端 30s ping / 客户端 heartbeat → pong；超时踢连接
- 帧分发：JSON 控制帧 + 二进制音频帧（累积到 PcmRingBuffer）
- 半双工：audio_end 触发 HalfDuplex.process → 下行音频帧 + reply_done
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

from .audio import PcmRingBuffer
from .config import VoiceConfig
from .e2ee import E2EELike, build_e2ee
from .half_duplex import HalfDuplex
from .schemas import (
    AudioChunk,
    chunk_payload,
    decode_audio_frame,
    encode_audio_frame,
    is_audio_frame,
)

logger = logging.getLogger(__name__)

CTRL_OK = {"hello", "audio_start", "audio_end", "wake", "cancel", "heartbeat", "speech_start", "speech_end", "interrupt"}


class VoiceSession:
    """单个手机连接会话（帧状态 + 缓冲 + 引擎引用）"""

    def __init__(self, ws: WebSocket, device_id: str, cfg: VoiceConfig, engine: HalfDuplex,
                 e2ee: E2EELike | None = None) -> None:
        self.ws = ws
        self.device_id = device_id
        self.cfg = cfg
        self.engine = engine
        self.buffer = PcmRingBuffer(cfg.session.buffer_max_bytes)
        self.state: str = "monitoring"
        self.up_seq = 0
        self.down_seq = 0
        self.last_rx = time.time()
        self.interrupted = False
        self.closed = False
        self.e2ee = e2ee
        # M3 云端全双工（MiniCPM-o Realtime API）：path=apm 时启用流式模式
        self.stream_mode: bool = cfg.path == "apm"
        self.bridge: Any = None   # ApmBridge 引用（run_session 装配）
        # v0.5.1 说完判定补静音：手机持续发帧（含底噪）→ 模型 VAD 判定不了"你说完"→ 永不回复。
        # 检测到停顿(>1.2s 低能量)时向云端补 2s 纯静音，让模型判定说完并回复（全双工下用户再开口仍可打断）
        self._last_voice_ts = time.time()
        self._silence_padded = False

    # ---------- 发送 ----------
    async def send_json(self, obj: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def send_state(self, state: str, ts: float | None = None) -> None:
        self.state = state
        await self.send_json({"type": "session_state", "state": state, "ts": ts or time.time()})

    async def send_error(self, code: str, message: str) -> None:
        await self.send_json({"type": "error", "code": code, "message": message})

    # ---------- M3 全双工下行（ApmBridge 回调） ----------
    async def send_apm_audio(self, pcm_s16: bytes) -> None:
        """APM 下行音频帧（16k s16 PCM → 现有二进制帧协议 0x02 头）"""
        if self.closed:
            return
        try:
            await self.ws.send_bytes(encode_audio_frame(self.down_seq, int(time.time() * 1000), pcm_s16))
            self.down_seq += 1
        except Exception:  # noqa: BLE001 - 连接断开
            pass

    async def send_apm_text(self, text: str) -> None:
        """APM 下行文本增量（transcript 控制帧，兼容现有协议）"""
        if self.closed:
            return
        try:
            await self.send_json({"type": "transcript", "text": text, "is_final": False})
        except Exception:  # noqa: BLE001
            pass

    async def send_apm_state(self, state: str) -> None:
        if self.closed:
            return
        await self.send_state(state)

    async def send_audio_down(self, audio_bytes: bytes, ts_ms: int, audio_format: str) -> None:
        """下行音频：audio_start 控制帧 + 二进制帧流 + audio_end（E2EE 时加密 payload）"""
        await self.send_json({"type": "audio_start", "format": audio_format, "seq": self.down_seq})
        for chunk in chunk_payload(audio_bytes):
            if self.interrupted:
                break
            payload = self.e2ee.encrypt_audio(self.down_seq, ts_ms, chunk) if self.e2ee else chunk
            await self.ws.send_bytes(encode_audio_frame(self.down_seq, ts_ms, payload))
            self.down_seq += 1
        await self.send_json({"type": "audio_end", "seq": self.down_seq - 1,
                              "reason": "interrupted" if self.interrupted else "done"})

    # ---------- 上行处理 ----------
    def on_audio(self, chunk: AudioChunk) -> None:
        if self.e2ee is not None:
            try:
                chunk = AudioChunk(
                    chunk.seq, chunk.ts_ms,
                    self.e2ee.decrypt_audio(chunk.seq, chunk.ts_ms, chunk.payload),
                )
            except ValueError as e:
                # 密钥/seq 不匹配：丢弃该帧并告警，不回退明文（spec §11-6 降级语义）
                logger.warning("e2ee decrypt failed, drop up frame seq=%s: %s", chunk.seq, e)
                return
        self.up_seq = chunk.seq
        self.last_rx = time.time()
        self.buffer.append(chunk.payload)
        if self.state == "monitoring":
            self.state = "listening"

    async def on_control(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        self.last_rx = time.time()
        if mtype == "heartbeat":
            await self.send_json({"type": "pong", "ts": msg.get("ts", time.time())})
        elif mtype == "cancel":
            self.buffer.clear()
            self.interrupted = True
            await self.send_state("monitoring")
        elif mtype == "interrupt":
            self.interrupted = True
            await self.send_state("listening")
        elif mtype in ("audio_start", "speech_start", "wake"):
            await self.send_state("listening")
        elif mtype in ("audio_end", "speech_end"):
            # speech_end：手机/中继链路（spec §7）结束帧；audio_end：LAN 链路结束帧——同一轮处理
            await self._process_round(msg)

    async def _process_round(self, msg: dict[str, Any]) -> None:
        """半双工一轮：STT → brain/local → TTS → 下行"""
        await self.send_state("thinking")
        await self.send_json({"type": "transcript", "text": "", "is_final": False})
        pcm = self.buffer.drain()
        self.interrupted = False
        result = await self.engine.process(pcm)
        if not result.ok:
            code = result.error_code or "internal"
            await self.send_error(code, result.meta.get("message", "处理失败"))
            await self.send_state("monitoring")
            return
        await self.send_json({"type": "transcript", "text": result.text, "is_final": True})
        await self.send_state("speaking")
        await self.send_audio_down(result.audio_bytes, int(time.time() * 1000), result.tts_format)
        await self.send_json({"type": "reply_done", "route": result.route,
                              "text": result.reply_text, "ts": time.time()})
        self.interrupted = False
        await self.send_state("monitoring")


class VoiceSessionManager:
    """连接注册表：单设备互斥 + 状态查询 + 心跳清理"""

    def __init__(self, cfg: VoiceConfig, engine: HalfDuplex) -> None:
        self._cfg = cfg
        self._engine = engine
        self._sessions: dict[str, VoiceSession] = {}

    @property
    def engine(self) -> HalfDuplex:
        return self._engine

    def register(self, session: VoiceSession) -> VoiceSession | None:
        """注册新会话；返回被踢的旧会话（同 device_id 互斥）"""
        old = self._sessions.get(session.device_id)
        self._sessions[session.device_id] = session
        return old

    def unregister(self, session: VoiceSession) -> None:
        if self._sessions.get(session.device_id) is session:
            self._sessions.pop(session.device_id, None)

    def get(self, device_id: str) -> VoiceSession | None:
        return self._sessions.get(device_id)

    def status(self) -> dict:
        sessions = []
        for s in self._sessions.values():
            stt_ok = False
            if hasattr(s.engine, "_stt"):
                try:
                    stt_ok = bool(s.engine._stt.available())
                except Exception:  # noqa: BLE001
                    stt_ok = False
            sessions.append({
                "device_id": s.device_id,
                "state": s.state,
                "path": "A" if s.stream_mode else "B",
                "engine": "apm_realtime" if s.stream_mode else "half_duplex",
                "stt_model_ok": stt_ok,
                "buffer_bytes": s.buffer.size,
                "last_rx": s.last_rx,
            })
        return {"online": len(self._sessions), "sessions": sessions}


async def kick_and_close(session: VoiceSession) -> None:
    """踢旧连接：发错误帧后关闭"""
    try:
        await session.send_error("kicked", "设备已由新连接替换")
        await session.ws.close(code=1000, reason="replaced by new connection")
    except Exception:  # noqa: BLE001 - 旧连接可能已断开
        pass
    session.closed = True


async def handshake(ws: WebSocket, cfg: VoiceConfig, manager: VoiceSessionManager) -> VoiceSession | None:
    """握手：等待 hello → token 校验 → 建会话（互斥踢旧在 run_session 内）→ 返回会话

    返回 None 表示握手失败（已关闭连接）。
    """
    await ws.accept()
    query_token = (ws.query_params or {}).get("token", "")
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=cfg.session.hello_timeout_s)
    except Exception:  # noqa: BLE001 - 超时/断开/二进制帧
        await ws.close(code=1008, reason="hello timeout")
        return None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await ws.send_text(json.dumps({"type": "error", "code": "bad_frame", "message": "hello 必须为 JSON"}))
        await ws.close(code=1008, reason="bad hello")
        return None
    if msg.get("type") != "hello":
        await ws.send_text(json.dumps({"type": "error", "code": "bad_frame", "message": "首帧必须为 hello"}))
        await ws.close(code=1008, reason="first frame must be hello")
        return None
    token = query_token or msg.get("token", "")
    if cfg.require_token and token != cfg.token:
        await ws.send_text(json.dumps({"type": "error", "code": "auth_failed", "message": "token 无效"}))
        await ws.close(code=1008, reason="auth failed")
        return None
    if not cfg.require_token:
        logger.warning("VOICE_TOKEN 未配置，语音网关处于开发态（无鉴权）")
    device_id = str(msg.get("device_id") or msg.get("device") or "android")
    # LAN 直连 E2EE：客户端 hello 声明 features 含 "e2ee" 且网关已配 VOICE_E2EE_KEY → 启用
    e2ee = build_e2ee(cfg, msg.get("features") or [])
    return VoiceSession(ws, device_id, cfg, manager.engine, e2ee=e2ee)


async def run_session(session: VoiceSession, manager: VoiceSessionManager) -> None:
    """会话主循环：注册 → ready → 收帧分发；心跳与断线清理"""
    old = manager.register(session)
    if old is not None and old is not session:
        await kick_and_close(old)
    # M3 全双工：装配 ApmBridge（云端引擎）
    # v0.5.1: 不再显式 start() —— 懒初始化（feed_pcm 首个音频块才建云会话）。
    # 旧逻辑 PC 常驻连接一建立就建会话，空闲被 MiniCPM-o 服务端回收（recv end/超时），
    # 用户说话时连接已死 → 音频全丢 → "已连接但不回话"（2026-08-06 现场实锤）
    bridge = None
    if session.stream_mode:
        try:
            from .apm_bridge import ApmBridge

            bridge = ApmBridge(
                on_audio_out=session.send_apm_audio,
                on_text=session.send_apm_text,
                on_state=session.send_apm_state,
                system_prompt=session.cfg.apm.system_prompt if hasattr(session.cfg, "apm") else "",
            )
            session.bridge = bridge
        except Exception as e:  # noqa: BLE001
            logger.warning("apm bridge create failed: %s", e)
            await session.send_error("apm_unavailable", f"云端语音引擎不可用: {e}")
            manager.unregister(session)
            session.closed = True
            return
    await session.send_json({
        "type": "ready", "session_id": f"vs-{int(time.time())}-{session.device_id}",
        "audio": {"up": "pcm_s16le_16k", "down": "mp3_24k"},
    })
    heartbeat_task = asyncio.create_task(_heartbeat_loop(session, manager))
    try:
        while True:
            msg = await session.ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] == "websocket.receive":
                if msg.get("text") is not None:
                    await _dispatch_text(session, msg["text"])
                elif msg.get("bytes") is not None:
                    await _dispatch_bytes(session, msg["bytes"])
    except Exception as e:  # noqa: BLE001 - 断开/异常统一清理
        logger.info("voice session %s closed: %s", session.device_id, e)
    finally:
        heartbeat_task.cancel()
        if bridge is not None:
            try:
                await bridge.close()
            except Exception:  # noqa: BLE001
                pass
        manager.unregister(session)
        session.closed = True


async def _dispatch_text(session: VoiceSession, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await session.send_error("bad_frame", "控制帧必须为 JSON")
        return
    mtype = msg.get("type")
    if mtype not in CTRL_OK:
        await session.send_error("bad_frame", f"未知控制帧类型: {mtype}")
        return
    if session.stream_mode:
        # M3 全双工：audio_end/speech_end 无 turn 概念（全双工靠静音判定），忽略；
        # heartbeat/wake/cancel/interrupt 仍走统一处理
        if mtype in ("audio_end", "speech_end"):
            return
    await session.on_control(msg)


async def _dispatch_bytes(session: VoiceSession, data: bytes) -> None:
    if not is_audio_frame(data):
        await session.send_error("bad_frame", "二进制帧必须为音频帧（0x02 开头）")
        return
    try:
        chunk = decode_audio_frame(data)
    except ValueError as e:
        await session.send_error("bad_frame", str(e))
        return
    if session.stream_mode and session.bridge is not None:
        # M3 全双工：音频帧直接流式转发云端 API（不累积 buffer、不等 audio_end）
        session.up_seq = chunk.seq
        session.last_rx = time.time()
        try:
            await _feed_apm_with_end_detect(session, chunk.payload)
        except Exception:  # noqa: BLE001
            pass
        return
    session.on_audio(chunk)


def _pcm_rms(s16: bytes) -> float:
    """16bit s16 PCM 平均能量（说话通常 >500；静音底噪 <200）"""
    if not s16:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(s16[: len(s16) // 2 * 2])
    return sum(abs(x) for x in samples) / len(samples) if samples else 0.0


async def _feed_apm_with_end_detect(session: VoiceSession, chunk: bytes) -> None:
    """v0.5.1：停顿补静音 —— 手机持续发帧导致模型判定不了"你说完"（2026-08-06 实锤）。
    低能量持续 >1.2s → 向云端补 2s 纯静音（一次性），模型 VAD 判定说完开始回复；
    用户再开口（能量回升）→ 重置状态，正常 feed（全双工 barge-in 不受影响）。"""
    now = time.time()
    if _pcm_rms(chunk) > 400.0:
        # 有声音：更新最后语音时间，清补静音标记
        session._last_voice_ts = now
        session._silence_padded = False
        await session.bridge.feed_pcm(chunk)
        return
    # 静音帧：若已停顿 >1.2s 且未补过 → 补 2s 纯静音（说完标记）
    if not session._silence_padded and (now - session._last_voice_ts) > 1.2:
        session._silence_padded = True
        await session.bridge.feed_pcm(b"\x00\x00" * 16000 * 2)
    else:
        await session.bridge.feed_pcm(chunk)


async def _heartbeat_loop(session: VoiceSession, manager: VoiceSessionManager) -> None:
    """服务端心跳：30s ping；若超时未收到任何帧则踢连接"""
    cfg = session.cfg
    while not session.closed:
        await asyncio.sleep(cfg.session.heartbeat_interval_s)
        if session.closed:
            break
        try:
            await session.send_json({"type": "ping", "ts": time.time()})
        except Exception:  # noqa: BLE001
            break
        if time.time() - session.last_rx > cfg.session.heartbeat_timeout_s:
            logger.info("voice heartbeat timeout, closing %s", session.device_id)
            # 必须主动 close WS：只 break 会留下僵尸连接（客户端 TCP 仍 ESTABLISHED 无感知，
            # 网关也不再发任何帧）——2026-08-05 现场僵死连接；close 后客户端立即感知并重连
            session.closed = True
            try:
                await session.ws.close(code=1001, reason="heartbeat timeout")
            except Exception:  # noqa: BLE001 - 连接可能已断开
                pass
            break
