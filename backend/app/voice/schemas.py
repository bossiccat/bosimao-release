"""语音网关 WS 帧协议（mobile-voice-spec §7 统一帧格式）

控制帧 = WS 文本帧（JSON，VoiceControlFrame 枚举类型）
音频帧 = WS 二进制帧：[0x02][seq:u32 BE][ts_ms:u64 BE][payload]

- 上行（手机 → PC）：hello / audio_start / audio_end / wake / cancel / heartbeat / speech_start / speech_end / interrupt
- 下行（PC → 手机）：ready / session_state / transcript / audio_start / audio_end / reply_done / error / pong
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Literal

# 二进制音频帧头：magic(1) + seq(4) + ts_ms(8) = 13 字节
AUDIO_FRAME_MAGIC = 0x02
AUDIO_FRAME_HEADER = struct.Struct(">BIQ")  # B=magic, I=seq u32 BE, Q=ts_ms u64 BE
AUDIO_FRAME_HEADER_LEN = AUDIO_FRAME_HEADER.size

# 下行单帧 payload 上限（WS 帧不宜过大，控制内存）
MAX_AUDIO_CHUNK_BYTES = 64 * 1024

# 控制帧类型（协议真源，见 mobile-voice-spec §7.3）
UpFrameType = Literal[
    "hello", "audio_start", "audio_end", "wake", "cancel",
    "heartbeat", "speech_start", "speech_end", "interrupt",
]
DownFrameType = Literal[
    "ready", "session_state", "transcript", "audio_start", "audio_end",
    "reply_done", "error", "pong",
]

# 会话状态（对齐 PRD pet_state 子集）
SessionState = Literal["monitoring", "listening", "thinking", "speaking", "idle"]


@dataclass
class AudioChunk:
    """解码后的音频帧"""

    seq: int
    ts_ms: int
    payload: bytes


@dataclass
class VoiceSessionState:
    """一个语音会话的运行时状态（供 /status 与控制面查询）"""

    device_id: str
    role: str = "phone"
    state: SessionState = "monitoring"
    path: str = "B"                      # A=原生全双工(M3) / B=半双工(M2)
    engine: str = "half_duplex"
    stt_model_ok: bool = False
    up_seq: int = 0
    down_seq: int = 0
    started_at: float = 0.0
    last_frame_at: float = 0.0
    buffer_bytes: int = 0
    meta: dict = field(default_factory=dict)


def encode_audio_frame(seq: int, ts_ms: int, payload: bytes) -> bytes:
    """编码下行音频帧（§7.2 二进制帧格式）"""
    return AUDIO_FRAME_HEADER.pack(AUDIO_FRAME_MAGIC, seq & 0xFFFFFFFF, ts_ms & 0xFFFFFFFFFFFFFFFF) + payload


def decode_audio_frame(data: bytes) -> AudioChunk:
    """解码上行音频帧；头部非法/过短抛 ValueError"""
    if len(data) < AUDIO_FRAME_HEADER_LEN:
        raise ValueError(f"audio frame too short: {len(data)} < {AUDIO_FRAME_HEADER_LEN}")
    magic, seq, ts_ms = AUDIO_FRAME_HEADER.unpack_from(data, 0)
    if magic != AUDIO_FRAME_MAGIC:
        raise ValueError(f"bad audio frame magic: {magic:#x}")
    return AudioChunk(seq=seq, ts_ms=ts_ms, payload=data[AUDIO_FRAME_HEADER_LEN:])


def is_audio_frame(data: bytes) -> bool:
    """判断 WS 二进制帧是否为本协议音频帧"""
    return len(data) >= AUDIO_FRAME_HEADER_LEN and data[0] == AUDIO_FRAME_MAGIC


def chunk_payload(payload: bytes, chunk_size: int = MAX_AUDIO_CHUNK_BYTES) -> list[bytes]:
    """把大块音频拆成 ≤chunk_size 的分片（下行逐帧发送）"""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    return [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
