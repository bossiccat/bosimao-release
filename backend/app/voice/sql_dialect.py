"""SQL 方言层：把 SQLite / CloudBase PostgreSQL 的真实差异收敛到一处。

为什么需要它（而不是在各仓库里写 if/else）：
仓库逻辑必须**只写一份**。SQLite 与 PG 在这几处有硬差异，任何一处散落进仓库
代码，就会长出第二套实现（本项目已经因为 `deploy/backend` 手工副本吃过生产
事故）。本模块把差异显式列出，仓库只面向 `SqlDialect` 编程。

1. 占位符：SQLite `?` vs PG `%s`
2. 时间：SQLite 存 Unix float vs PG `timestamptz`（云端 schema 全为 timestamptz）
3. 自增主键取回：SQLite `cursor.lastrowid` vs PG `INSERT ... RETURNING`
4. 事务：SQLite `BEGIN IMMEDIATE` vs PG 协议层 `Connection.transaction()`
5. JSON：SQLite 存 TEXT 字符串 vs PG `jsonb`

约束：PG 侧**不得在导入期引入 psycopg**（离线环境没有安装，且既有契约测试
`test_injected_pool_factory_never_imports_psycopg` 会抓），psycopg 只在本模块
函数体内惰性导入。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, ContextManager, Iterator, Protocol, runtime_checkable

__all__ = [
    "SQLITE",
    "POSTGRES",
    "SqlDialect",
    "SqliteDialect",
    "PostgresDialect",
    "SQLITE_DIALECT",
    "POSTGRES_DIALECT",
    "get_dialect",
]

SQLITE = "sqlite"
POSTGRES = "postgres"


# ---------------------------------------------------------------------------
# 时间归一 helpers（两条路径共用，保证 dataclass 字段类型一致：float / None）
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """任意存储形态（float / datetime / ISO 字符串）→ Unix 秒。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.timestamp()
    if isinstance(value, bool):
        raise TypeError(f"timestamp must not be bool, got {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _as_datetime(value: Any) -> datetime | None:
    """Unix 秒 / datetime → aware UTC datetime（timestamptz 唯一合法入参）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(_as_float(value), tz=timezone.utc)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SqlDialect(Protocol):
    """SQL 方言。**接口刻意保持小而显式**：每条方法对应一处真实方言差异。"""

    name: str
    """方言名：`SQLITE` / `POSTGRES`。"""

    placeholder: str
    """参数占位符：SQLite `?`、PG `%s`。"""

    supports_returning: bool
    """是否支持 `INSERT ... RETURNING`（PG True，SQLite 走 lastrowid）。"""

    def timestamp_to_storage(self, value: Any) -> Any:
        """时间戳列入参：SQLite → float；PG → aware datetime。"""
        ...

    def timestamp_from_storage(self, value: Any) -> float | None:
        """时间戳列出参归一成 Unix 秒，保证上层 dataclass 字段类型一致。"""
        ...

    def json_to_storage(self, value: Any, *, sort_keys: bool = False,
                        separators: tuple[str, str] | None = None) -> Any:
        """JSON 列入参：SQLite → JSON 字符串；PG → psycopg `Json` 包装。"""
        ...

    def json_from_storage(self, value: Any) -> Any:
        """JSON 列出参归一成 Python 对象（SQLite 解字符串，PG 已是 dict/list）。"""
        ...

    def now_expr(self) -> str:
        """SQL 里的"当前时间"表达式：`CURRENT_TIMESTAMP` / `now()`。"""
        ...

    def begin_sql(self, *, immediate: bool = True) -> str | None:
        """事务开始语句。PG 返回 None：BEGIN 由 `Connection.transaction()` 在协议层发出。"""
        ...

    def transaction(self, conn: Any) -> ContextManager[Any]:
        """方言化事务上下文：退出即提交，抛异常即回滚。"""
        ...

    def insert_returning(self, sql: str, returning: str) -> str:
        """给 INSERT 追加主键取回子句（PG `RETURNING`；SQLite 原样返回）。"""
        ...

    def last_insert_id(self, cursor: Any) -> int | None:
        """取回刚插入的自增主键（SQLite `lastrowid`；PG 读 RETURNING 行）。"""
        ...

    def is_unique_violation(self, exc: BaseException) -> bool:
        """该异常是否为唯一约束冲突。

        用于「插入即占位」这类语义（如 nonce 一次性消费）：只有真正的唯一冲突
        才能解释为「已被占用」。其余异常必须向上抛——曾有代码 catch Exception
        后一律返回 False，把连接失败/类型错误全部伪装成「nonce 重复」，
        既掩盖真因又给出错误业务码。
        """
        ...


# ---------------------------------------------------------------------------
# 实现
# ---------------------------------------------------------------------------


class SqliteDialect:
    """SQLite 方言：Unix float 时间、TEXT JSON、`BEGIN IMMEDIATE`。"""

    name = SQLITE
    placeholder = "?"
    supports_returning = False

    def timestamp_to_storage(self, value: Any) -> Any:
        return _as_float(value)

    def timestamp_from_storage(self, value: Any) -> float | None:
        return _as_float(value)

    def json_to_storage(self, value: Any, *, sort_keys: bool = False,
                        separators: tuple[str, str] | None = None) -> Any:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys,
                          separators=separators)

    def json_from_storage(self, value: Any) -> Any:
        if isinstance(value, (str, bytes, bytearray)):
            return json.loads(value)
        return value

    def now_expr(self) -> str:
        return "CURRENT_TIMESTAMP"

    def begin_sql(self, *, immediate: bool = True) -> str | None:
        return "BEGIN IMMEDIATE" if immediate else "BEGIN"

    @contextmanager
    def transaction(self, conn: Any) -> Iterator[Any]:
        conn.execute(self.begin_sql())
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    def insert_returning(self, sql: str, returning: str) -> str:
        # SQLite 3.35+ 才支持 RETURNING；存量路径统一走 lastrowid，避免版本分叉。
        return sql

    def last_insert_id(self, cursor: Any) -> int | None:
        rowid = getattr(cursor, "lastrowid", None)
        return None if rowid is None else int(rowid)

    def is_unique_violation(self, exc: BaseException) -> bool:
        import sqlite3

        return isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc).upper()


class PostgresDialect:
    """PostgreSQL 方言：`%s` 占位符、timestamptz、jsonb、协议层事务。"""

    name = POSTGRES
    placeholder = "%s"
    supports_returning = True

    def timestamp_to_storage(self, value: Any) -> Any:
        return _as_datetime(value)

    def timestamp_from_storage(self, value: Any) -> float | None:
        return _as_float(value)

    def json_to_storage(self, value: Any, *, sort_keys: bool = False,
                        separators: tuple[str, str] | None = None) -> Any:
        if value is None:
            return None
        # 惰性导入：导入期不得依赖 psycopg（离线环境未安装）。
        from psycopg.types.json import Json  # noqa: PLC0415

        return Json(value)

    def json_from_storage(self, value: Any) -> Any:
        # psycopg 已把 jsonb 解析成 dict/list；字符串形态（fake/降级）兜底解析。
        if isinstance(value, (str, bytes, bytearray)):
            return json.loads(value)
        return value

    def now_expr(self) -> str:
        return "now()"

    def begin_sql(self, *, immediate: bool = True) -> str | None:
        # PG 无 BEGIN IMMEDIATE；隐式/协议层 BEGIN 即默认隔离级别。
        return None

    @contextmanager
    def transaction(self, conn: Any) -> Iterator[Any]:
        with conn.transaction():
            yield conn

    def insert_returning(self, sql: str, returning: str) -> str:
        return f"{sql.rstrip().rstrip(';')} RETURNING {returning}"

    def last_insert_id(self, cursor: Any) -> int | None:
        row = cursor.fetchone()
        if row is None:
            return None
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        return None if value is None else int(value)

    def is_unique_violation(self, exc: BaseException) -> bool:
        try:
            from psycopg import errors as pg_errors
        except Exception:  # pragma: no cover - psycopg 缺失时无法判定
            return False
        return isinstance(exc, pg_errors.UniqueViolation)


SQLITE_DIALECT = SqliteDialect()
POSTGRES_DIALECT = PostgresDialect()

_DIALECTS: dict[str, SqlDialect] = {
    SQLITE: SQLITE_DIALECT,
    POSTGRES: POSTGRES_DIALECT,
}


def get_dialect(name: str) -> SqlDialect:
    """按名字取方言单例；未知方言 fail-closed（不静默退回 SQLite）。"""
    try:
        return _DIALECTS[name]
    except KeyError:
        raise ValueError(f"unknown SQL dialect: {name!r}") from None
