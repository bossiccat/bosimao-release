"""仓库与存储公共工具：时间戳、敏感字段校验。"""
from __future__ import annotations

import time

# 敏感键/值：日志、审计与事件元数据一律禁止出现（SPEC 9.1 / QA spec §5.2-10）
SENSITIVE_KEYS = {
    "secret",
    "credential_secret",
    "credential_hash",
    "nonce",
    "nonce_hash",
    "user_sig",
    "usersig",
    "sdk_secret",
    "secret_key",
    "secretkey",
    "pairing_code",
    "token",
    "password",
    "passphrase",
    "transcript",
    "audio",
    "raw_audio",
    "ciphertext",
}
SENSITIVE_VALUE_MARKERS = (
    "-----BEGIN",
    "userSig",
    "usersig",
    "nonce",
)


def now_unix(now: float | None = None) -> float:
    return time.time() if now is None else now


def assert_redacted_json(value: object) -> None:
    """metadata_redacted_json 白名单校验：敏感键或敏感值直接拒绝（fail-closed）"""
    if not isinstance(value, dict):
        raise ValueError("审计 metadata 必须是 JSON 对象")
    for key, item in value.items():
        lowered = str(key).lower()
        if lowered in SENSITIVE_KEYS or any(marker in lowered for marker in SENSITIVE_VALUE_MARKERS):
            raise ValueError(f"审计元数据禁止敏感键: {key}")
        if isinstance(item, str):
            text = item.lower()
            if any(marker.lower() in text for marker in ("credential_secret", "usersig", "user_sig")):
                raise ValueError(f"审计元数据禁止敏感值: {key}")
