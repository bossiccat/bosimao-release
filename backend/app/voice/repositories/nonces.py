"""consumed_nonces 仓库：主体绑定哈希、原子消费、TTL 清理。

nonce 只存主体 + 哈希；不同主体使用相同 nonce 字符串互不串扰（QA spec §5.2-9）。
同一份实现服务 SQLite 与 PostgreSQL，差异走 `self.dialect`。
"""
from __future__ import annotations

import hashlib

from .base import RepositoryBase
from .common import now_unix


def hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


class NonceRepository(RepositoryBase):
    def consume(self, subject_id: str, nonce: str, ttl_seconds: int = 300,
                now: float | None = None) -> bool:
        """原子消费：unique(subject_id, nonce_hash) 保证同主体同 nonce 只成功一次"""
        ts = now_unix(now)
        digest = hash_nonce(nonce)
        try:
            self._execute(
                "INSERT INTO consumed_nonces"
                " (subject_id, nonce_hash, expires_at, created_at, updated_at)"
                f" VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph})",
                (subject_id, digest,
                 self.dialect.timestamp_to_storage(ts + ttl_seconds),
                 self.dialect.timestamp_to_storage(ts),
                 self.dialect.timestamp_to_storage(ts)),
            )
        except Exception:
            # 唯一约束冲突 = 已被消费；方言层的回滚由 _txn 负责
            return False
        return True

    def purge_expired(self, now: float | None = None) -> int:
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        cursor = self._execute(
            f"DELETE FROM consumed_nonces WHERE expires_at < {self.ph}", (ts,)
        )
        return cursor.rowcount
