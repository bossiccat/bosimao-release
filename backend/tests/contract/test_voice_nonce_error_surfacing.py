"""nonce 消费的错误暴露契约。

真实事故（部署 007/009 期间定位）：`NonceRepository.consume()` 里
`except Exception: return False` 把**所有**异常都解释成「nonce 已被消费」，
于是连接失败、类型不匹配、权限问题一律对外表现为 `40102 nonce_replay`。
结果：真因被完全掩盖，排查方向被带偏到「重放」这条完全错误的路上。

本文件钉死正确语义：
- 唯一约束冲突 → False（真的重复消费）
- 其它任何异常 → 必须向上抛，不得伪装成重复消费
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.voice.repositories.nonces import NonceRepository  # noqa: E402
from app.voice.sql_dialect import POSTGRES_DIALECT, SQLITE_DIALECT  # noqa: E402


class _Cursor:
    def __init__(self) -> None:
        self.rowcount = 1


class _Conn:
    """最小连接替身：execute 由注入的 callable 决定行为。"""

    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour

    def execute(self, sql, params=()):  # noqa: ANN001
        if self._behaviour is not None:
            raise self._behaviour
        return _Cursor()

    # 方言的 transaction() 需要提交/回滚（SQLite 路径走 BEGIN IMMEDIATE + commit）。
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _repo(dialect, behaviour=None) -> NonceRepository:
    from contextlib import contextmanager

    @contextmanager
    def connect():
        yield _Conn(behaviour)

    return NonceRepository(connect, dialect)


# --- 方言分类 ---------------------------------------------------------------


def test_sqlite_dialect_classifies_unique_violation() -> None:
    assert SQLITE_DIALECT.is_unique_violation(
        sqlite3.IntegrityError("UNIQUE constraint failed: consumed_nonces.nonce_hash")
    )
    assert not SQLITE_DIALECT.is_unique_violation(sqlite3.OperationalError("no such table"))


def test_postgres_dialect_never_mislabels_unrelated_errors() -> None:
    """psycopg 不可用时也必须返回 False，绝不能把任意异常当唯一冲突。"""
    assert not POSTGRES_DIALECT.is_unique_violation(RuntimeError("connection refused"))
    assert not POSTGRES_DIALECT.is_unique_violation(ValueError("bad type"))


# --- consume() 的语义 -------------------------------------------------------


def test_consume_returns_false_only_for_unique_violation() -> None:
    repo = _repo(SQLITE_DIALECT, sqlite3.IntegrityError("UNIQUE constraint failed: x"))
    assert repo.consume("owner", "nonce-1") is False


def test_consume_reraises_storage_failures_instead_of_reporting_replay() -> None:
    """连接失败必须抛出——不能变成 40102「重放」。"""
    repo = _repo(SQLITE_DIALECT, sqlite3.OperationalError("unable to open database file"))
    with pytest.raises(sqlite3.OperationalError):
        repo.consume("owner", "nonce-2")


def test_consume_reraises_unexpected_errors() -> None:
    repo = _repo(SQLITE_DIALECT, RuntimeError("connection refused"))
    with pytest.raises(RuntimeError):
        repo.consume("owner", "nonce-3")


def test_consume_succeeds_when_insert_goes_through() -> None:
    repo = _repo(SQLITE_DIALECT, None)
    assert repo.consume("owner", "nonce-4") is True
