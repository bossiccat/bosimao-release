"""rate_limit_buckets 仓库：固定窗口计数，唯一(subject_id, route_key, window_start)。

device/IP/route 三类键均以 subject_id + route_key 表达；窗口由调用者计算。
同一份实现服务 SQLite 与 PostgreSQL（两条方言的 UPSERT 语法一致）。
"""
from __future__ import annotations

from .base import RepositoryBase
from .common import now_unix


class RateLimitRepository(RepositoryBase):
    def increment(self, subject_id: str, route_key: str, window_start: float,
                  now: float | None = None) -> int:
        """原子递增当前窗口计数，返回更新后的计数（首请求计数为 1）"""
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO rate_limit_buckets"
                " (subject_id, route_key, window_start, count, created_at, updated_at)"
                f" VALUES ({self.ph}, {self.ph}, {self.ph}, 1, {self.ph}, {self.ph})"
                " ON CONFLICT(subject_id, route_key, window_start)"
                # PG 里裸写 `count = count + 1` 会报
                # AmbiguousColumn: column reference "count" is ambiguous
                # （分不清目标表列还是 excluded 列）。必须限定为表名.列名；
                # 该写法 SQLite 也接受，因此两个方言共用同一句 SQL。
                " DO UPDATE SET count = rate_limit_buckets.count + 1,"
                " updated_at = excluded.updated_at",
                (subject_id, route_key, self.dialect.timestamp_to_storage(window_start),
                 ts, ts),
            )
            row = conn.execute(
                "SELECT count FROM rate_limit_buckets"
                f" WHERE subject_id = {self.ph} AND route_key = {self.ph}"
                f" AND window_start = {self.ph}",
                (subject_id, route_key, self.dialect.timestamp_to_storage(window_start)),
            ).fetchone()
            return int(row["count"])

    def purge_expired(self, cutoff: float, now: float | None = None) -> int:
        cursor = self._execute(
            f"DELETE FROM rate_limit_buckets WHERE window_start < {self.ph}",
            (self.dialect.timestamp_to_storage(cutoff),),
        )
        return cursor.rowcount
