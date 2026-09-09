"""仓库基类：连接租借 + 方言化事务的唯一入口。

`connect` 是**上下文管理器工厂**（SQLite `VoiceStore.connect` 与 PG
`PostgresVoiceStore.connect` 都是 `@contextmanager`）：退出即归还/关闭，
不允许把连接透传出去或缓存。

写操作一律走 `_txn()`（方言决定 `BEGIN IMMEDIATE` 还是 `conn.transaction()`），
读操作走 `_session()`（不抢写锁）。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

__all__ = ["RepositoryBase"]


class RepositoryBase:
    def __init__(self, connect: Any, dialect: Any) -> None:
        self._connect = connect
        self.dialect = dialect

    @property
    def ph(self) -> str:
        """占位符简写：SQL 一律用 f-string 拼它，杜绝两种方言两套字符串。"""
        return self.dialect.placeholder

    @contextmanager
    def _session(self) -> Iterator[Any]:
        """只读会话：借连接 → 归还，不开显式事务。"""
        with self._connect() as conn:
            yield conn

    @contextmanager
    def _txn(self) -> Iterator[Any]:
        """写事务：退出提交，抛异常回滚。"""
        with self._connect() as conn:
            with self.dialect.transaction(conn):
                yield conn

    def _execute(self, sql: str, params: tuple = ()) -> Any:
        with self._txn() as conn:
            return conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple = ()) -> Any:
        with self._session() as conn:
            return conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        with self._session() as conn:
            return conn.execute(sql, params).fetchall()
