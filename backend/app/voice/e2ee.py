"""LAN 直连 E2EE 装配（与 App VoiceCipher 对齐，实现复用 relay.relay_protocol.RelayE2EE）

- AAD = seq 8B 大端；payload = [iv 12B][AES-GCM 密文+tag 16B]；密钥 = SHA-256(passphrase) 派生
- hello features 声明 "e2ee" 且网关已配 VOICE_E2EE_KEY → 返回 RelayE2EE 实例
- 纯装配，不含会话/业务逻辑（session.py 只负责在帧路径上调用加解密）
"""
from __future__ import annotations

import logging
from typing import Protocol

from .config import VoiceConfig

logger = logging.getLogger(__name__)


class E2EELike(Protocol):
    """LAN 直连 E2EE 最小接口（实现复用 relay.relay_protocol.RelayE2EE，保证与 App 同规则）"""

    def encrypt_audio(self, seq: int, ts_ms: int, payload: bytes) -> bytes: ...
    def decrypt_audio(self, seq: int, ts_ms: int, data: bytes) -> bytes: ...


def build_e2ee(cfg: VoiceConfig, features: list) -> E2EELike | None:
    """按客户端 hello features + 网关配置决定是否启用 E2EE；返回 None = 明文路径"""
    if "e2ee" not in (features or []):
        return None
    if not cfg.e2ee_key:
        logger.warning("客户端声明 e2ee 但网关未配置 VOICE_E2EE_KEY，按明文处理（无法解密）")
        return None
    from relay.relay_protocol import RelayE2EE

    return RelayE2EE(cfg.e2ee_key)
