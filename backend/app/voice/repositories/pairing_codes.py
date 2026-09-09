"""pairing_codes 仓库：创建、元数据读取、原子消费、过期清理。

只处理数据存取，不包含业务规则（SPEC §6）。
同一份实现服务 SQLite 与 PostgreSQL，差异走 `self.dialect`。
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from .base import RepositoryBase
from .common import now_unix


@dataclass
class PairingCodeMeta:
    """配对码元数据（不含明文 pairing_code）"""

    code_hash: str
    created_by_owner_id: str
    platform: str
    expires_at: float
    consumed_at: float | None
    consumed_device_id: str | None
    created_at: float


def hash_code(code: str) -> str:
    """pairing_code 只保存抗离线攻击的 SHA-256 哈希（SPEC 9.1）"""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_pairing_code() -> str:
    """生成随机一次性 bootstrap secret（>=20 字符，OpenAPI minLength=20）"""
    return secrets.token_urlsafe(24)


class PairingCodeRepository(RepositoryBase):
    """pairing_codes 表存取；consume 使用 compare-and-update 保证原子单次消费"""

    def create(
        self,
        owner_id: str,
        platform: str,
        ttl_seconds: int,
        device_name_hint: str | None = None,
        now: float | None = None,
    ) -> tuple[str, dict]:
        """创建一次性配对码：明文只返回给调用者一次，库中只存 code_hash"""
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("pairing TTL 必须在 (0, 300] 秒内")
        code = generate_pairing_code()
        ts = now_unix(now)
        self._execute(
            "INSERT INTO pairing_codes"
            " (code_hash, created_by_owner_id, device_name_hint, platform,"
            "  expires_at, created_at, updated_at)"
            f" VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph},"
            f" {self.ph}, {self.ph})",
            (hash_code(code), owner_id, device_name_hint, platform,
             self.dialect.timestamp_to_storage(ts + ttl_seconds),
             self.dialect.timestamp_to_storage(ts),
             self.dialect.timestamp_to_storage(ts)),
        )
        return code, {
            "pairing_code": code,
            "expires_at": ts + ttl_seconds,
            "max_uses": 1,
            "ttl_seconds": ttl_seconds,
        }

    def get_meta(self, code: str) -> PairingCodeMeta | None:
        row = self._fetchone(
            "SELECT code_hash, created_by_owner_id, platform, expires_at,"
            f" consumed_at, consumed_device_id, created_at"
            f" FROM pairing_codes WHERE code_hash = {self.ph}",
            (hash_code(code),),
        )
        if row is None:
            return None
        ts = self.dialect.timestamp_from_storage
        return PairingCodeMeta(
            code_hash=row["code_hash"],
            created_by_owner_id=row["created_by_owner_id"],
            platform=row["platform"],
            expires_at=ts(row["expires_at"]),
            consumed_at=ts(row["consumed_at"]),
            consumed_device_id=row["consumed_device_id"],
            created_at=ts(row["created_at"]),
        )

    def consume(self, code: str, device_id: str, now: float | None = None) -> bool:
        """原子 compare-and-update：只有未消费且未过期的记录被消费成功"""
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        cursor = self._execute(
            "UPDATE pairing_codes SET consumed_at ="
            f" {self.ph}, consumed_device_id = {self.ph}, updated_at = {self.ph}"
            f" WHERE code_hash = {self.ph} AND consumed_at IS NULL"
            f" AND expires_at > {self.ph}",
            (ts, device_id, ts, hash_code(code), ts),
        )
        return cursor.rowcount == 1

    def purge_expired(self, now: float | None = None) -> int:
        """清理已过期（未消费）与已消费超过保留期的记录"""
        ts = self.dialect.timestamp_to_storage(now_unix(now) - 86400)
        cursor = self._execute(
            f"DELETE FROM pairing_codes WHERE expires_at < {self.ph}", (ts,)
        )
        return cursor.rowcount
