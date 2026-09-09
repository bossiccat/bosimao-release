"""SQLite / PostgreSQL 仓库门面对齐 —— 方言层契约（TDD）。

目标：`PostgresVoiceStore` 必须与 SQLite `VoiceStore` 具备**同名同签名**的仓库
门面，且仓库逻辑**只写一份**（共享 repository + 共享 facade mixin）。差异只允许
出现在 `app.voice.sql_dialect` 里。

本文件**不连真实数据库**、**不依赖 psycopg 是否安装**（本环境就没有）：PG 连接
通过 `pool=` 注入手写 fake，断言落在**真实被执行的 SQL 串与绑定参数**上，而不
是"mock 被调用了几次"。

覆盖的方言差异（至少三条真实 SQL）：device_credentials / consumed_nonces /
rate_limit_buckets。
"""
from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# backend/tests/contract 下没有 __init__.py，pytest 不会自动把 backend/ 放进 sys.path，
# 与 test_sidecar_renderer_cors_contract.py 的做法一致，显式补上。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.voice.pg_storage import (  # noqa: E402
    PostgresVoiceStore,
    VoiceStoreProtocol,
)
from app.voice.storage import VoiceStore  # noqa: E402

PG_STORAGE_MODULE = "app.voice.pg_storage"
VALID_DSN = "postgresql://jax:secret@db.internal:5432/jax_voice"

NOW = 1_700_000_000.0
EXPIRES_AT = NOW + 30 * 86400

REPOSITORY_ATTRIBUTES = (
    "pairing_codes",
    "pending_sessions",
    "hello_proofs",
    "device_credentials",
    "nonces",
    "rate_limit",
    "audit",
    "settings",
)


# ---------------------------------------------------------------------------
# fake psycopg 连接池：手写，不是 MagicMock。记录真实被执行的 SQL 与绑定参数。
# ---------------------------------------------------------------------------


def _norm(sql: object) -> str:
    return sql if isinstance(sql, str) else str(sql)


class FakeCursor:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool
        self.rowcount = 1

    def fetchone(self):
        return self._pool.rows

    def fetchall(self):
        return [self._pool.rows] if self._pool.rows is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeConnection:
    """模拟 psycopg3 Connection 的可观测子集。"""

    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool
        self.row_factory = None

    def execute(self, sql, params=None):
        self.pool.sql.append(_norm(sql))
        self.pool.params.append(tuple(params) if params else ())
        return FakeCursor(self.pool)

    @contextmanager
    def transaction(self):
        # 协议层 BEGIN/COMMIT：绝不产生 "BEGIN IMMEDIATE" 这种 SQLite 专有语句。
        self.pool.transactions += 1
        try:
            yield self
        finally:
            self.pool.commits += 1

    def commit(self):  # PG 路径不该走手动 commit
        self.pool.manual_commits.append("commit")

    def rollback(self):
        self.pool.manual_commits.append("rollback")


class FakePool:
    def __init__(self, rows=None) -> None:
        self.sql: list[str] = []
        self.params: list[tuple] = []
        self.transactions = 0
        self.commits = 0
        self.manual_commits: list[str] = []
        self.rows = rows if rows is not None else {"count": 1}

    @contextmanager
    def connection(self):
        yield FakeConnection(self)

    def flat_params(self) -> list:
        return [value for params in self.params for value in params]

    def sql_text(self) -> str:
        return "\n".join(self.sql).upper()


def _pg_store(rows=None):
    pool = FakePool(rows=rows)
    return PostgresVoiceStore(dsn=VALID_DSN, pool=pool), pool


@contextmanager
def _fake_psycopg_json(monkeypatch):
    """离线环境没有 psycopg：注入最小 `psycopg.types.json.Json` 供绑定值断言。

    被测的生产代码仍然是"惰性导入 psycopg、不做静默降级"；这里只是替契约测试
    提供驱动适配器，断言的是**绑定值的形态**（Jsonb 包装 vs TEXT 字符串）。
    """
    import types as _types

    class Json:
        def __init__(self, obj) -> None:
            self.obj = obj

        def __repr__(self) -> str:
            return f"Json({self.obj!r})"

    json_mod = _types.ModuleType("psycopg.types.json")
    json_mod.Json = Json
    types_mod = _types.ModuleType("psycopg.types")
    types_mod.json = json_mod
    pkg = _types.ModuleType("psycopg")
    pkg.types = types_mod
    monkeypatch.setitem(sys.modules, "psycopg", pkg)
    monkeypatch.setitem(sys.modules, "psycopg.types", types_mod)
    monkeypatch.setitem(sys.modules, "psycopg.types.json", json_mod)
    yield Json


# ---------------------------------------------------------------------------
# 1. PG 路径：SQL 必须是合法 PG（无 ? 占位符、无 BEGIN IMMEDIATE）
# ---------------------------------------------------------------------------


def test_pg_device_credentials_sql_has_no_qmark_and_no_begin_immediate():
    store, pool = _pg_store()

    store.device_credentials.save("dev-1", "cred-1", "phone", "android",
                                  "secret-plain", EXPIRES_AT, now=NOW)

    assert pool.sql, "必须真的发出了 SQL（fake 未记录到任何语句）"
    for sql in pool.sql:
        assert "?" not in sql, f"PG 路径出现 SQLite 占位符: {sql}"
        assert "BEGIN IMMEDIATE" not in sql.upper(), f"PG 路径出现 SQLite 专有事务: {sql}"
    assert any("INSERT INTO DEVICE_CREDENTIALS" in sql.upper() for sql in pool.sql)
    assert any("%S" in sql.upper() for sql in pool.sql)


def test_pg_nonces_and_rate_limit_sql_use_percent_s_placeholders():
    store, pool = _pg_store()

    store.consume_nonce("dev-1", "nonce-abc", ttl_seconds=300, now=NOW)
    store.rate_limit.increment("dev-1", "/v1/voice/session/pending", NOW, now=NOW)

    joined = pool.sql_text()
    assert "INSERT INTO CONSUMED_NONCES" in joined
    assert "INSERT INTO RATE_LIMIT_BUCKETS" in joined
    assert "?" not in joined, f"PG 路径出现 SQLite 占位符:\n{joined}"
    assert "BEGIN IMMEDIATE" not in joined
    assert pool.transactions >= 1, "PG 必须走 conn.transaction()，而不是手写 BEGIN"


def test_pg_pending_session_enqueue_emits_pg_placeholders():
    store, pool = _pg_store()

    store.enqueue_pending_session("sess-1", "dev-1", "room-1", 1, EXPIRES_AT, now=NOW)

    joined = pool.sql_text()
    assert "INSERT INTO PENDING_SESSION_CLAIMS" in joined
    assert "?" not in joined
    assert "BEGIN IMMEDIATE" not in joined


# ---------------------------------------------------------------------------
# 2. PG 路径：时间列必须绑 aware datetime，不是裸 float
# ---------------------------------------------------------------------------


def test_pg_binds_aware_datetimes_never_bare_floats():
    store, pool = _pg_store()

    store.device_credentials.save("dev-1", "cred-1", "phone", "android",
                                  "secret-plain", EXPIRES_AT, now=NOW)
    store.consume_nonce("dev-1", "nonce-abc", ttl_seconds=300, now=NOW)
    store.rate_limit.increment("dev-1", "/r", NOW, now=NOW)

    flat = pool.flat_params()
    assert flat, "必须真的绑定了参数"
    assert any(
        isinstance(value, datetime) and value.tzinfo is not None for value in flat
    ), f"时间列必须绑 aware datetime，实际参数: {flat}"
    assert not any(
        isinstance(value, float) for value in flat
    ), f"timestamptz 列不得绑裸 float，实际参数: {flat}"


def test_pg_audit_binds_jsonb_wrapper_and_aware_datetime(monkeypatch):
    """PG 的 jsonb 列必须绑驱动适配器（psycopg `Json`），而不是 SQLite 的 TEXT 字符串。"""
    with _fake_psycopg_json(monkeypatch) as json_adapter:
        store, pool = _pg_store()
        store.write_audit("device.register", "device", "dev-1", "ok",
                          {"platform": "android"}, now=NOW)

    flat = pool.flat_params()
    assert not any(isinstance(value, float) for value in flat), flat
    assert any(isinstance(value, datetime) and value.tzinfo is not None
               for value in flat), flat
    assert any(isinstance(value, json_adapter) for value in flat), flat
    assert not any(isinstance(value, str) and value.startswith("{") for value in flat), flat


# ---------------------------------------------------------------------------
# 3. 门面对齐：同名同签名（防以后漏加）
# ---------------------------------------------------------------------------


def _public_methods(cls) -> set[str]:
    return {
        name for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name, None))
    }


def test_postgres_store_exposes_every_sqlite_facade_method_with_same_signature():
    sqlite_methods = _public_methods(VoiceStore)
    pg_methods = _public_methods(PostgresVoiceStore)

    missing = sorted(sqlite_methods - pg_methods)
    assert not missing, f"PostgresVoiceStore 缺少门面方法: {missing}"

    mismatched = {
        name: (
            str(inspect.signature(getattr(VoiceStore, name))),
            str(inspect.signature(getattr(PostgresVoiceStore, name))),
        )
        for name in sorted(sqlite_methods & pg_methods)
        if inspect.signature(getattr(VoiceStore, name))
        != inspect.signature(getattr(PostgresVoiceStore, name))
    }
    assert not mismatched, f"门面方法签名不一致: {mismatched}"


def test_both_stores_expose_the_same_eight_repository_classes():
    sqlite_store = VoiceStore(":memory:")
    pg_store, _pool = _pg_store()

    for name in REPOSITORY_ATTRIBUTES:
        assert hasattr(sqlite_store, name), f"SQLite 门面缺仓库属性: {name}"
        assert hasattr(pg_store, name), f"PG 门面缺仓库属性: {name}"
        assert type(getattr(sqlite_store, name)) is type(getattr(pg_store, name)), (
            f"{name} 在两条路径上不是同一份实现"
        )


def test_postgres_store_satisfies_full_voice_store_protocol():
    _store, _pool = _pg_store()
    assert isinstance(_store, VoiceStoreProtocol)


# ---------------------------------------------------------------------------
# 4. SQLite 路径不回归：仍然是 ? 占位符 + BEGIN IMMEDIATE
# ---------------------------------------------------------------------------


class RecordingConnection:
    """真实 sqlite3 连接的记录代理：捕获**未展开**的 SQL 串与绑定参数。

    不用 `set_trace_callback`：它给的是参数已展开后的语句，`?` 占位符会消失，
    断言会变成假绿。
    """

    def __init__(self, real, captured: list) -> None:
        self._real = real
        self._captured = captured

    def execute(self, sql, params=()):
        self._captured.append((str(sql), tuple(params) if params else ()))
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _recording_sqlite_store(monkeypatch, tmp_path: Path, captured: list):
    from app.voice import storage as storage_mod

    original = storage_mod._open_connection

    def recording(db_path: str):
        return RecordingConnection(original(db_path), captured)

    monkeypatch.setattr(storage_mod, "_open_connection", recording)
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    captured.clear()
    return store


def test_sqlite_path_still_emits_qmark_and_begin_immediate(monkeypatch, tmp_path):
    captured: list = []
    store = _recording_sqlite_store(monkeypatch, tmp_path, captured)

    store.device_credentials.save("dev-1", "cred-1", "phone", "android",
                                  "secret-plain", EXPIRES_AT, now=NOW)
    store.consume_nonce("dev-1", "nonce-abc", ttl_seconds=300, now=NOW)
    store.rate_limit.increment("dev-1", "/r", NOW, now=NOW)

    statements = [sql for sql, _params in captured]
    joined = "\n".join(statements).upper()
    assert "INSERT INTO DEVICE_CREDENTIALS" in joined
    assert "INSERT INTO CONSUMED_NONCES" in joined
    assert "INSERT INTO RATE_LIMIT_BUCKETS" in joined
    assert "BEGIN IMMEDIATE" in joined, f"SQLite 路径丢了 BEGIN IMMEDIATE:\n{captured}"
    assert "?" in joined, "SQLite 路径丢了 ? 占位符"
    assert "%S" not in joined, f"SQLite 路径混入 PG 占位符:\n{captured}"


def test_sqlite_path_still_binds_float_timestamps(monkeypatch, tmp_path):
    captured: list = []
    store = _recording_sqlite_store(monkeypatch, tmp_path, captured)

    store.device_credentials.save("dev-1", "cred-1", "phone", "android",
                                  "secret-plain", EXPIRES_AT, now=NOW)
    store.write_audit("device.register", "device", "dev-1", "ok",
                      {"platform": "android"}, now=NOW)

    flat = [value for _sql, params in captured for value in params]
    assert any(isinstance(value, float) for value in flat), flat
    assert not any(isinstance(value, datetime) for value in flat), flat
    # metadata 在 SQLite 侧仍是 JSON 字符串（jsonb 是 PG 侧的形态）
    assert any(isinstance(value, str) and value.startswith("{") for value in flat), flat


def test_sqlite_path_still_returns_float_timestamps(monkeypatch, tmp_path):
    captured: list = []
    store = _recording_sqlite_store(monkeypatch, tmp_path, captured)

    store.save_device("dev-1", "secret-plain", expires_at=EXPIRES_AT, now=NOW)
    row = store.get_device("dev-1")

    assert row is not None
    assert isinstance(row.expires_at, float), row.expires_at
    assert row.expires_at == EXPIRES_AT
