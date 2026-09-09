"""wake userSig 静态加密封装（AES-256-GCM + 版本化 + AAD 上下文绑定）。

用途：把 control_plane_wake_events 里的 userSig（TRTC 进房签名凭据）以密文形态
落库——SQLite 侧存 BLOB、CloudBase PostgreSQL 侧存 bytea
（cloudbase/migrations/20260908154838_voice_control_plane.sql:241-259 的两列
user_sig_ciphertext bytea NOT NULL / user_sig_encryption_version text NOT NULL）。

安全边界（务必阅读后再改本文件）：
1. **密钥只由构造参数注入**：本模块不硬编码密钥、不读任何全局单例/环境变量、
   不提供默认密钥兜底。缺密钥、密钥非 bytes、长度不足 32 字节 → 构造即抛
   UserSigCipherKeyError（fail-closed）。生产环境的密钥必须由 KMS /
   Secret Manager / 部署环境变量注入后显式传入。
2. **AEAD**：AES-256-GCM，96-bit 随机 nonce，128-bit tag。密文布局 =
   magic(4B) + version(1B) + nonce(12B) + (ciphertext||tag)。版本号用于未来密钥
   轮转时的多版本共存；轮转后旧密文必须用**对应版本的密钥实例**解密。
3. **上下文绑定（AAD）**：device_id / prior_session_id / new_session_id /
   prior_generation / new_generation / wake_event_id 全部进入 AAD。
   任何一项不匹配（即密文被挪到别的会话/别的事务上下文）解密一律失败。
4. **fail-closed**：密文篡改、AAD 不匹配、未知版本、版本与密钥实例不匹配、
   长度截断、UTF-8 解不出明文 → 全抛 UserSigDecryptionError；**绝不返回空串、
   None 或部分数据**让调用方继续跑。
5. **不提供**的能力：密钥轮转编排、超出 wake_event_id 幂等范围的抗重放、
   前向安全（PFS）、以及网络传输层保护（那层由 TLS 负责）。本模块只解决
   at-rest confidentiality + 密文-上下文绑定。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "CIPHER_VERSION",
    "MAGIC",
    "KEY_BYTES",
    "UserSigCipher",
    "UserSigCipherError",
    "UserSigCipherKeyError",
    "UserSigCiphertextMissing",
    "UserSigDecryptionError",
    "build_user_sig_cipher",
]

MAGIC = b"JXUS"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
CIPHER_VERSION = "v1"

_HEADER_BYTES = len(MAGIC) + 1
_MIN_BLOB_BYTES = _HEADER_BYTES + NONCE_BYTES + TAG_BYTES

_VERSION_TO_TAG = {CIPHER_VERSION: 0x01}
_TAG_TO_VERSION = {0x01: CIPHER_VERSION}

_BYTES_TYPES = (bytes, bytearray, memoryview)


class UserSigCipherError(Exception):
    """userSig 加解密基类错误。异常消息一律不得包含明文 userSig。"""


class UserSigCipherKeyError(UserSigCipherError):
    """密钥缺失/格式非法/长度不足/版本不受支持 → fail-closed。"""


class UserSigCiphertextMissing(UserSigCipherError):
    """库里的密文缺失或类型不对（如遗留明文行）→ fail-closed。"""


class UserSigDecryptionError(UserSigCipherError):
    """解密失败（篡改 / AAD 不匹配 / 未知版本 / 截断）→ fail-closed。"""


def _canonical_aad(*, device_id: Any, prior_session_id: Any, new_session_id: Any,
                   prior_generation: Any, new_generation: Any,
                   wake_event_id: Any) -> bytes:
    """AAD 的确定性序列化：字段统一规范化后按 key 排序，避免同值不同表示绕过绑定。"""
    payload = {
        "device_id": str(device_id),
        "prior_session_id": str(prior_session_id),
        "new_session_id": str(new_session_id),
        "prior_generation": int(prior_generation),
        "new_generation": int(new_generation),
        "wake_event_id": str(wake_event_id),
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class UserSigCipher:
    """wake userSig 的 AEAD 加解密器（AES-256-GCM）。"""

    def __init__(self, key: Any, *, version: str = CIPHER_VERSION) -> None:
        if version not in _VERSION_TO_TAG:
            raise UserSigCipherKeyError(f"unsupported user_sig cipher version: {version!r}")
        if not isinstance(key, _BYTES_TYPES):
            raise UserSigCipherKeyError("user_sig cipher key must be raw bytes")
        raw = bytes(key)
        if len(raw) != KEY_BYTES:
            raise UserSigCipherKeyError(
                f"user_sig cipher key must be exactly {KEY_BYTES} bytes"
            )
        self._aes = AESGCM(raw)
        self._version = version

    @classmethod
    def from_base64_key(cls, value: Any, *, version: str = CIPHER_VERSION) -> "UserSigCipher":
        """从 base64 编码的密钥串构造（配 KMS/Secret 注入的场景）。"""
        if not isinstance(value, str) or not value.strip():
            raise UserSigCipherKeyError("user_sig cipher key is missing")
        try:
            raw = base64.b64decode(value.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
            raise UserSigCipherKeyError("user_sig cipher key is not valid base64") from exc
        return cls(raw, version=version)

    @property
    def version(self) -> str:
        return self._version

    def encrypt(self, user_sig: Any, *, device_id: Any, prior_session_id: Any,
                new_session_id: Any, prior_generation: Any, new_generation: Any,
                wake_event_id: Any) -> bytes:
        if not isinstance(user_sig, str) or not user_sig:
            raise UserSigCipherError("user_sig must be a non-empty string")
        nonce = _random_nonce()
        aad = _canonical_aad(
            device_id=device_id, prior_session_id=prior_session_id,
            new_session_id=new_session_id, prior_generation=prior_generation,
            new_generation=new_generation, wake_event_id=wake_event_id,
        )
        ciphertext = self._aes.encrypt(nonce, user_sig.encode("utf-8"), aad)
        return MAGIC + bytes([_VERSION_TO_TAG[self._version]]) + nonce + ciphertext

    def decrypt(self, blob: Any, *, device_id: Any, prior_session_id: Any,
                new_session_id: Any, prior_generation: Any, new_generation: Any,
                wake_event_id: Any) -> str:
        if not isinstance(blob, _BYTES_TYPES):
            raise UserSigCiphertextMissing("user_sig ciphertext is missing")
        raw = bytes(blob)
        if len(raw) < _MIN_BLOB_BYTES:
            raise UserSigDecryptionError("user_sig ciphertext is truncated")
        if raw[:len(MAGIC)] != MAGIC:
            raise UserSigDecryptionError("user_sig ciphertext header is invalid")
        version_byte = raw[len(MAGIC)]
        version = _TAG_TO_VERSION.get(version_byte)
        if version is None:
            raise UserSigDecryptionError(
                f"unknown user_sig encryption version: 0x{version_byte:02x}"
            )
        if version != self._version:
            raise UserSigDecryptionError(
                "user_sig encryption version does not match the injected key version"
            )
        nonce = raw[_HEADER_BYTES:_HEADER_BYTES + NONCE_BYTES]
        ciphertext = raw[_HEADER_BYTES + NONCE_BYTES:]
        aad = _canonical_aad(
            device_id=device_id, prior_session_id=prior_session_id,
            new_session_id=new_session_id, prior_generation=prior_generation,
            new_generation=new_generation, wake_event_id=wake_event_id,
        )
        try:
            plaintext = self._aes.decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise UserSigDecryptionError(
                "user_sig decryption failed: ciphertext tampered or context mismatch"
            ) from exc
        try:
            user_sig = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UserSigDecryptionError("decrypted user_sig is not valid utf-8") from exc
        if not user_sig:
            raise UserSigDecryptionError("decrypted user_sig is empty")
        return user_sig


def build_user_sig_cipher(raw_key: Any, *, logger: Any = None) -> "UserSigCipher | None":
    """从注入的配置（base64 的 32 字节密钥）构造加密器。

    配置为空视为「未配置密钥」→ 返回 None，调用方（ledger）会 fail-closed 拒绝
    wake 签发，绝不退化成明文落库。配置了但格式非法 → 直接抛
    UserSigCipherKeyError（启动期暴露错误，不等到写库时才炸）。
    """
    value = raw_key.strip() if isinstance(raw_key, str) else ""
    if not value:
        if logger is not None:
            logger.warning(
                "voice_user_sig_cipher_key is not configured;"
                " wake signing fails closed until a 32-byte base64 key is injected"
            )
        return None
    cipher = UserSigCipher.from_base64_key(value)
    if logger is not None:
        logger.info("wake user_sig cipher enabled (version=%s)", cipher.version)
    return cipher


def _random_nonce() -> bytes:
    return os.urandom(NONCE_BYTES)
