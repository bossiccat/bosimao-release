"""限流 upsert 的方言安全契约。

真实事故（首次连上云端 PostgreSQL 时暴露）：`rate_limit.increment()` 的
`ON CONFLICT ... DO UPDATE SET count = count + 1` 在 SQLite 下工作正常，但 PG 报

    AmbiguousColumn: column reference "count" is ambiguous

因为 PG 无法判断自增引用的是目标表列还是 `excluded` 列。必须限定为表名.列名。
这类问题只会在真库上暴露，本文件把它钉进 CI。
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.voice.repositories.rate_limit import RateLimitRepository  # noqa: E402
from app.voice.sql_dialect import POSTGRES_DIALECT, SQLITE_DIALECT  # noqa: E402


class _Cursor:
    rowcount = 1

    def fetchone(self):
        return {"count": 1}


class _Conn:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def execute(self, sql, params=()):  # noqa: ANN001
        self._sink.append(" ".join(str(sql).split()))
        return _Cursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    @contextmanager
    def transaction(self):
        """PG 方言的 transaction() 走 libpq 的 conn.transaction()。"""
        yield self


def _capture(dialect) -> list[str]:
    statements: list[str] = []

    @contextmanager
    def connect():
        yield _Conn(statements)

    RateLimitRepository(connect, dialect).increment("subject", "route", 1_700_000_000.0)
    return statements


def test_upsert_qualifies_the_self_reference_in_both_dialects() -> None:
    for dialect in (SQLITE_DIALECT, POSTGRES_DIALECT):
        sql = " ".join(_capture(dialect))
        assert "rate_limit_buckets.count + 1" in sql, (
            f"{dialect.name}: 自增列必须写成 表名.列名，否则 PG 报 AmbiguousColumn"
        )


def test_upsert_never_contains_a_bare_self_reference() -> None:
    """裸 `count = count + 1` 在 SQLite 能跑、在 PG 报错——必须彻底禁止。"""
    ambiguous = re.compile(r"set\s+count\s*=\s*count\s*\+", re.IGNORECASE)
    for dialect in (SQLITE_DIALECT, POSTGRES_DIALECT):
        sql = " ".join(_capture(dialect))
        assert ambiguous.search(sql) is None, f"{dialect.name}: 出现有歧义的裸列自增"


def test_upsert_conflict_target_matches_the_unique_constraint() -> None:
    sql = " ".join(_capture(POSTGRES_DIALECT))
    assert "on conflict(subject_id, route_key, window_start)" in sql.lower()
