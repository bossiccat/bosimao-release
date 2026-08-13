"""settings 仓库：隐私开关等键值对（settings 表，unique(key)）"""
from __future__ import annotations

from .common import now_unix


class SettingsRepository:
    def __init__(self, connect):
        self._connect = connect

    def get(self, key: str) -> str | None:
        row = self._connect().execute(
            "SELECT value_encrypted FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else None

    def set(self, key: str, value: str, now: float | None = None) -> None:
        ts = now_unix(now)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value_encrypted, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value_encrypted = excluded.value_encrypted,"
                " updated_at = excluded.updated_at",
                (key, value, ts, ts),
            )
