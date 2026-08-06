"""中继协议（mobile-voice-spec §7 帧格式 + M2 配对/E2EE 扩展）

- 控制帧 = WS 文本帧（JSON）：pair / paired / peer_joined / peer_left / heartbeat / pong / error / kick
- 音频帧 = WS 二进制帧：[0x02][seq:u32 BE][ts_ms:u64 BE][payload]
- E2EE：AES-256-GCM 预共享密钥；与 App VoiceCipher 对齐：
  - 密钥：32 字节 AES 密钥，可来自 32B base64（RELAY_E2EE_KEY 直传）或
    passphrase 经 SHA-256 派生（App VoiceCipher.deriveKey 同构）
  - payload = nonce(12) || 密文 || tag(16)；AAD = seq 仅 8 字节大端（App seqBytes 同构）
  - ts_ms 仅存帧头，不参与 AAD（App 侧同规则）
- 本模块独立于 app（relay 可单独启动），帧编解码与 app.voice.schemas 同构但自包含。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 二进制音频帧头：magic(1) + seq(4) + ts_ms(8) = 13 字节（与 spec §7.2 一致）
AUDIO_FRAME_MAGIC = 0x02
AUDIO_FRAME_HEADER = struct.Struct(">BIQ")
AUDIO_FRAME_HEADER_LEN = AUDIO_FRAME_HEADER.size

MAX_AUDIO_CHUNK_BYTES = 64 * 1024
E2EE_NONCE_LEN = 12
E2EE_TAG_LEN = 16
E2EE_AAD_LEN = 8  # AAD = seq 8 字节大端（App VoiceCipher.seqBytes 同构）

# App 默认开发密钥（VoiceConfig.DEFAULT_E2EE_KEY）；PC 侧可用它直接派生同一 32B 密钥
DEFAULT_E2EE_PASSPHRASE = "jax-voice-dev-e2ee-20260803-0001"

# 配对帧（M2）：{"type":"pair","role":"phone|pc","device_id":"...","pairing_code":"..."}
PAIR_ROLES = ("phone", "pc")
CTRL_TYPES = {"pair", "paired", "peer_joined", "peer_left", "heartbeat", "pong", "error", "kick"}


@dataclass
class AudioChunk:
    seq: int
    ts_ms: int
    payload: bytes


# ---------- 帧编解码 ----------

def encode_audio_frame(seq: int, ts_ms: int, payload: bytes) -> bytes:
    return AUDIO_FRAME_HEADER.pack(AUDIO_FRAME_MAGIC, seq & 0xFFFFFFFF, ts_ms & 0xFFFFFFFFFFFFFFFF) + payload


def decode_audio_frame(data: bytes) -> AudioChunk:
    if len(data) < AUDIO_FRAME_HEADER_LEN:
        raise ValueError(f"audio frame too short: {len(data)} < {AUDIO_FRAME_HEADER_LEN}")
    magic, seq, ts_ms = AUDIO_FRAME_HEADER.unpack_from(data, 0)
    if magic != AUDIO_FRAME_MAGIC:
        raise ValueError(f"bad audio frame magic: {magic:#x}")
    return AudioChunk(seq=seq, ts_ms=ts_ms, payload=data[AUDIO_FRAME_HEADER_LEN:])


def is_audio_frame(data: bytes) -> bool:
    return len(data) >= AUDIO_FRAME_HEADER_LEN and data[0] == AUDIO_FRAME_MAGIC


def chunk_payload(payload: bytes, chunk_size: int = MAX_AUDIO_CHUNK_BYTES) -> list[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    return [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]


# ---------- 配对帧 ----------

def make_pair_frame(role: str, device_id: str, pairing_code: str, token: str = "") -> str:
    """构造配对帧（token 可放查询参数，也可随帧，二选一）"""
    if role not in PAIR_ROLES:
        raise ValueError(f"role must be one of {PAIR_ROLES}")
    msg = {"type": "pair", "role": role, "device_id": device_id, "pairing_code": pairing_code}
    if token:
        msg["token"] = token
    return json.dumps(msg, ensure_ascii=False)


def parse_pair_frame(raw: str) -> dict:
    """解析配对帧；非法抛 ValueError"""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"pair frame must be JSON: {e}") from e
    if msg.get("type") != "pair":
        raise ValueError("first frame must be pair")
    if msg.get("role") not in PAIR_ROLES:
        raise ValueError(f"role must be one of {PAIR_ROLES}")
    if not msg.get("device_id"):
        raise ValueError("device_id required")
    if not msg.get("pairing_code"):
        raise ValueError("pairing_code required")
    return msg


# ---------- E2EE（AES-256-GCM，AAD = seq 8B，与 App VoiceCipher 对齐） ----------

def derive_key_from_passphrase(passphrase: str) -> bytes:
    """App VoiceCipher.deriveKey 同构：SHA-256(UTF-8) → 32 字节 AES 密钥"""
    if not passphrase or not passphrase.strip():
        raise ValueError("E2EE passphrase must not be blank")
    return hashlib.sha256(passphrase.encode("utf-8")).digest()


def load_e2ee_key(raw: str | None) -> bytes:
    """读取 E2EE 密钥（两种表示，与 App 对齐）：
    - 32 字节 base64（RELAY_E2EE_KEY 旧直传）→ 原样返回；
    - 明文 passphrase → SHA-256 派生（App VoiceCipher.deriveKey 同构）。
    缺失时生成开发密钥（调用方负责日志告警）。
    """
    if raw is None or not raw.strip():
        return os.urandom(32)
    s = raw.strip()
    try:
        key = base64.b64decode(s, validate=True)
    except Exception:  # noqa: BLE001 - 非法 base64 按 passphrase 处理
        key = b""
    if len(key) == 32:
        return key
    return derive_key_from_passphrase(s)


def gen_dev_key_b64() -> str:
    """生成开发密钥（base64 32 字节），仅供本地联调"""
    return base64.b64encode(os.urandom(32)).decode()


class RelayE2EE:
    """AES-256-GCM 音频 payload 加密（与 App VoiceCipher 对齐）

    - AAD = seq 仅 8 字节大端（u32 seq 值高位补零；App seqBytes 同构）→ 防重放/防篡改
    - payload = [iv 12B][AES-GCM 密文+tag 16B]；ts_ms 仅存帧头，不参与 AAD
    - encrypt_audio/decrypt_audio 保留 (seq, ts_ms, payload) 签名兼容旧调用方
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 密钥必须为 32 字节")
        self._aead = AESGCM(key)

    @staticmethod
    def _aad(seq: int) -> bytes:
        return struct.pack(">Q", seq & 0xFFFFFFFFFFFFFFFF)

    def encrypt_audio(self, seq: int, ts_ms: int, payload: bytes) -> bytes:
        """加密 payload：nonce(12) || 密文+tag(16)（AAD = seq 8B；ts_ms 不参与）"""
        nonce = secrets.token_bytes(E2EE_NONCE_LEN)
        ct = self._aead.encrypt(nonce, payload, self._aad(seq))
        return nonce + ct

    def decrypt_audio(self, seq: int, ts_ms: int, data: bytes) -> bytes:
        """解密 payload；AAD 不符/被篡改/重放 seq 抛 ValueError（ts_ms 不参与）"""
        if len(data) < E2EE_NONCE_LEN + E2EE_TAG_LEN:
            raise ValueError(f"e2ee payload too short: {len(data)}")
        nonce, ct = data[:E2EE_NONCE_LEN], data[E2EE_NONCE_LEN:]
        return self._aead.decrypt(nonce, ct, self._aad(seq))


class ReplayGuard:
    """按方向跟踪最近 seq，拒绝非递增（重放/乱序）"""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def check(self, direction: str, seq: int) -> None:
        if seq < 0:
            raise ValueError("seq must be >= 0")
        last = self._last.get(direction, -1)
        if seq <= last:
            raise ValueError(f"replay or out-of-order seq: {seq} <= {last} (dir={direction})")
        self._last[direction] = seq


def make_heartbeat(ts: float | None = None) -> str:
    import time as _time
    return json.dumps({"type": "heartbeat", "ts": ts if ts is not None else _time.time()}, ensure_ascii=False)


def make_error(code: str, message: str) -> str:
    return json.dumps({"type": "error", "code": code, "message": message}, ensure_ascii=False)
