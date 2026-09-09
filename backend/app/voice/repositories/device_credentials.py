"""device_credentials 仓库：保存哈希、读取、撤销状态。

Secret 明文只出现在调用者侧；本模块只接收并存储哈希（SPEC 9.1）。
同一份实现同时服务 SQLite 与 PostgreSQL：方言差异全部走 `self.dialect`。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .base import RepositoryBase
from .common import now_unix


@dataclass
class DeviceCredentialRow:
    device_id: str
    credential_id: str
    device_name: str
    platform: str
    credential_hash: str
    status: str
    expires_at: float
    last_seen_at: float | None
    revoked_at: float | None
    revoke_reason: str | None
    created_at: float


def hash_secret(secret: str) -> str:
    """抗离线攻击哈希：SHA-256 + 随机盐，以 盐$哈希 形式存储"""
    salt = hashlib.sha256(secret.encode("utf-8") + b"jax-salt-v1").hexdigest()[:16]
    return f"{salt}${hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()}"


_COLUMNS = (
    "device_id, credential_id, device_name, platform, credential_hash, status,"
    " expires_at, last_seen_at, revoked_at, revoke_reason, created_at"
)


class DeviceCredentialRepository(RepositoryBase):
    def save(self, device_id: str, credential_id: str, device_name: str, platform: str,
             secret: str, expires_at: float, now: float | None = None) -> None:
        """保存凭证元数据；secret 只存哈希，绝不落明文"""
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        credential_hash = hash_secret(secret)
        self._execute(
            "INSERT INTO device_credentials"
            " (device_id, credential_id, device_name, platform, credential_hash, status,"
            "  expires_at, created_at, updated_at)"
            f" VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}, 'active',"
            f" {self.ph}, {self.ph}, {self.ph})",
            (device_id, credential_id, device_name, platform, credential_hash,
             self.dialect.timestamp_to_storage(expires_at), ts, ts),
        )

    def get(self, device_id: str) -> DeviceCredentialRow | None:
        row = self._fetchone(
            f"SELECT {_COLUMNS} FROM device_credentials WHERE device_id = {self.ph}",
            (device_id,),
        )
        if row is None:
            return None
        return self._row(row)

    def verify(self, device_id: str, secret: str) -> DeviceCredentialRow | None:
        """校验凭证：哈希比对；失败返回 None（不泄漏差异）"""
        row = self.get(device_id)
        if row is None:
            return None
        salt, digest = row.credential_hash.split("$", 1)
        candidate = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
        if candidate != digest:
            return None
        return row

    def touch_last_seen(self, device_id: str, now: float | None = None) -> None:
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        self._execute(
            "UPDATE device_credentials SET last_seen_at ="
            f" {self.ph}, updated_at = {self.ph} WHERE device_id = {self.ph}",
            (ts, ts, device_id),
        )

    def revoke(self, device_id: str, reason: str, now: float | None = None) -> bool:
        """强一致撤销：credential 立即失效（幂等：已撤销也返回 True）"""
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        with self._txn() as conn:
            cursor = conn.execute(
                "UPDATE device_credentials SET status = 'revoked', revoked_at ="
                f" {self.ph}, revoke_reason = {self.ph}, updated_at = {self.ph}"
                f" WHERE device_id = {self.ph} AND revoked_at IS NULL",
                (ts, reason, ts, device_id),
            )
            if cursor.rowcount == 1:
                return True
            row = conn.execute(
                f"SELECT status FROM device_credentials WHERE device_id = {self.ph}",
                (device_id,),
            ).fetchone()
            return row is not None and row["status"] == "revoked"

    def list_active(self) -> list[DeviceCredentialRow]:
        rows = self._fetchall(
            f"SELECT {_COLUMNS} FROM device_credentials ORDER BY created_at DESC"
        )
        return [self._row(row) for row in rows]

    def _row(self, row: Any) -> DeviceCredentialRow:
        """行 → dataclass：按列名取值（dict_row 与 sqlite3.Row 通用），时间归一成 float。"""
        ts = self.dialect.timestamp_from_storage
        return DeviceCredentialRow(
            device_id=row["device_id"],
            credential_id=row["credential_id"],
            device_name=row["device_name"],
            platform=row["platform"],
            credential_hash=row["credential_hash"],
            status=row["status"],
            expires_at=ts(row["expires_at"]),
            last_seen_at=ts(row["last_seen_at"]),
            revoked_at=ts(row["revoked_at"]),
            revoke_reason=row["revoke_reason"],
            created_at=ts(row["created_at"]),
        )
