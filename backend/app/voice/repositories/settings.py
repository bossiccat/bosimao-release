"""settings 仓库：隐私开关等键值对（settings 表，unique(key)）"""
from __future__ import annotations

from .base import RepositoryBase
from .common import now_unix


class SettingsRepository(RepositoryBase):
    def get(self, key: str) -> str | None:
        row = self._fetchone(
            f"SELECT value_encrypted FROM settings WHERE key = {self.ph}", (key,)
        )
        return row["value_encrypted"] if row is not None else None

    def set(self, key: str, value: str, now: float | None = None) -> None:
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        self._execute(
            "INSERT INTO settings(key, value_encrypted, created_at, updated_at)"
            f" VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph})"
            " ON CONFLICT(key) DO UPDATE SET value_encrypted = excluded.value_encrypted,"
            " updated_at = excluded.updated_at",
            (key, value, ts, ts),
        )
