"""consumed_nonces 仓库：主体绑定哈希、原子消费、TTL 清理。

nonce 只存主体 + 哈希；不同主体使用相同 nonce 字符串互不串扰（QA spec §5.2-9）。
"""
from __future__ import annotations

import hashlib

from .common import now_unix


def hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


class NonceRepository:
    def __init__(self, connect):
        self._connect = connect

    def consume(self, subject_id: str, nonce: str, ttl_seconds: int = 300,
                now: float | None = None) -> bool:
        """原子消费：unique(subject_id, nonce_hash) 保证同主体同 nonce 只成功一次"""
        ts = now_unix(now)
        digest = hash_nonce(nonce)
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO consumed_nonces"
                    " (subject_id, nonce_hash, expires_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (subject_id, digest, ts + ttl_seconds, ts, ts),
                )
                return True
            except Exception:
                return False

    def purge_expired(self, now: float | None = None) -> int:
        ts = now_unix(now)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM consumed_nonces WHERE expires_at < ?", (ts,)
            )
            return cursor.rowcount
