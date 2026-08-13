"""device_credentials 仓库：保存哈希、读取、撤销状态。

Secret 明文只出现在调用者侧；本模块只接收并存储哈希（SPEC 9.1）。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

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


class DeviceCredentialRepository:
    def __init__(self, connect):
        self._connect = connect

    def save(self, device_id: str, credential_id: str, device_name: str, platform: str,
             secret: str, expires_at: float, now: float | None = None) -> None:
        """保存凭证元数据；secret 只存哈希，绝不落明文"""
        ts = now_unix(now)
        credential_hash = hash_secret(secret)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO device_credentials"
                " (device_id, credential_id, device_name, platform, credential_hash, status,"
                "  expires_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (device_id, credential_id, device_name, platform, credential_hash,
                 expires_at, ts, ts),
            )

    def get(self, device_id: str) -> DeviceCredentialRow | None:
        row = self._connect().execute(
            "SELECT device_id, credential_id, device_name, platform, credential_hash, status,"
            " expires_at, last_seen_at, revoked_at, revoke_reason, created_at"
            " FROM device_credentials WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        return DeviceCredentialRow(*row)

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
        ts = now_unix(now)
        with self._connect() as conn:
            conn.execute(
                "UPDATE device_credentials SET last_seen_at = ?, updated_at = ?"
                " WHERE device_id = ?",
                (ts, ts, device_id),
            )

    def revoke(self, device_id: str, reason: str, now: float | None = None) -> bool:
        """强一致撤销：credential 立即失效（幂等：已撤销也返回 True）"""
        ts = now_unix(now)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE device_credentials SET status = 'revoked', revoked_at = ?,"
                " revoke_reason = ?, updated_at = ? WHERE device_id = ?"
                " AND revoked_at IS NULL",
                (ts, reason, ts, device_id),
            )
            if cursor.rowcount == 1:
                return True
            row = conn.execute(
                "SELECT status FROM device_credentials WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            return row is not None and row[0] == "revoked"

    def list_active(self) -> list[DeviceCredentialRow]:
        rows = self._connect().execute(
            "SELECT device_id, credential_id, device_name, platform, credential_hash, status,"
            " expires_at, last_seen_at, revoked_at, revoke_reason, created_at"
            " FROM device_credentials ORDER BY created_at DESC"
        ).fetchall()
        return [DeviceCredentialRow(*row) for row in rows]
