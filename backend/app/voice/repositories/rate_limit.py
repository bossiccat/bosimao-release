"""rate_limit_buckets 仓库：固定窗口计数，唯一(subject_id, route_key, window_start)。

device/IP/route 三类键均以 subject_id + route_key 表达；窗口由调用者计算。
"""
from __future__ import annotations

from .common import now_unix


class RateLimitRepository:
    def __init__(self, connect):
        self._connect = connect

    def increment(self, subject_id: str, route_key: str, window_start: float,
                  now: float | None = None) -> int:
        """原子递增当前窗口计数，返回更新后的计数（首请求计数为 1）"""
        ts = now_unix(now)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rate_limit_buckets"
                " (subject_id, route_key, window_start, count, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(subject_id, route_key, window_start)"
                " DO UPDATE SET count = count + 1, updated_at = excluded.updated_at",
                (subject_id, route_key, window_start, ts, ts),
            )
            row = conn.execute(
                "SELECT count FROM rate_limit_buckets"
                " WHERE subject_id = ? AND route_key = ? AND window_start = ?",
                (subject_id, route_key, window_start),
            ).fetchone()
            return int(row[0])

    def purge_expired(self, cutoff: float, now: float | None = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM rate_limit_buckets WHERE window_start < ?", (cutoff,)
            )
            return cursor.rowcount
