"""PostgreSQL voice storage adapter — RED contract tests (TDD stage 1).

目标：把语音控制面的持久化从 SQLite 迁到 CloudBase PostgreSQL 之前，先用契约
把 adapter 的形状钉死。本文件**不连真实数据库**、**不依赖 psycopg 是否安装**：
pool 通过注入进入被测对象，语义断言落在"真实被执行的 SQL 串"上，而不是
"mock 被调用了几次"。

RED 阶段预期：全部失败，且失败原因必须是 `app.voice.pg_storage` 尚未实现。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

# backend/tests/contract 下没有 __init__.py，pytest 不会自动把 backend/ 放进 sys.path，
# 与 test_sidecar_renderer_cors_contract.py 的做法一致，显式补上。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.voice.storage import VoiceStore  # noqa: E402

PG_STORAGE_MODULE = "app.voice.pg_storage"
VALID_DSN = "postgresql://jax:secret@db.internal:5432/jax_voice"
VALID_DSN_ALT = "postgres://jax:secret@db.internal:5432/jax_voice"


def load_pg_storage():
    """惰性导入被测模块：RED 阶段这里抛 ModuleNotFoundError。"""
    return importlib.import_module(PG_STORAGE_MODULE)


# --------------------------------------------------------------------------
# fake psycopg pool：手写，不是 MagicMock。记录真实被执行（或协议层等效）的 SQL。
# --------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.pool.sql.append(_norm(sql))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _norm(sql) -> str:
    return sql if isinstance(sql, str) else str(sql)


class FakeConnection:
    """模拟 psycopg3 Connection 的可观测子集。"""

    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool
        self.row_factory = None
        self.returned = False

    def execute(self, sql, params=None):
        self.pool.sql.append(_norm(sql))
        return FakeCursor(self)

    def cursor(self):
        return FakeCursor(self)

    def commit(self) -> None:
        self.pool.sql.append("COMMIT")

    def rollback(self) -> None:
        self.pool.sql.append("ROLLBACK")

    @contextmanager
    def transaction(self):
        # psycopg3 的 transaction() 在 libpq 协议层发 BEGIN/COMMIT，不走 execute()。
        # fake 把它落成可观测的 SQL 记录，以便断言语义。
        self.execute("BEGIN")
        try:
            yield self
        except BaseException:
            self.execute("ROLLBACK")
            raise
        self.execute("COMMIT")

    def close(self) -> None:
        self.returned = True


class FakePool:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.close_calls = 0
        self.opened = 0
        self.returned = 0
        self.configs: list = []

    @contextmanager
    def connection(self):
        conn = FakeConnection(self)
        self.opened += 1
        try:
            yield conn
        finally:
            # psycopg pool.connection() 的 with 块：退出即归还 lease
            self.returned += 1
            conn.returned = True

    def close(self) -> None:
        self.close_calls += 1

    async def aclose(self) -> None:
        self.close_calls += 1


def make_store(dsn: str = VALID_DSN, pool: FakePool | None = None):
    """返回 (store, pool)。pool_factory 注入路径。"""
    mod = load_pg_storage()
    created = pool if pool is not None else FakePool()
    seen: list = []

    def factory(config):
        seen.append(config)
        created.configs.append(config)
        return created

    store = mod.PostgresVoiceStore(dsn=dsn, pool_factory=factory)
    store._factory_seen = seen  # 仅测试侧挂载，便于断言配置透传
    return store, created


# --------------------------------------------------------------------------
# A. VoiceStoreProtocol
# --------------------------------------------------------------------------


def test_voice_store_protocol_is_runtime_checkable_and_sqlite_satisfies_it(tmp_path):
    mod = load_pg_storage()
    proto = mod.VoiceStoreProtocol
    # runtime_checkable 的 Protocol 才能做 isinstance；非 runtime_checkable 会 TypeError
    assert isinstance(VoiceStore(tmp_path / "voice.db"), proto)


def test_voice_store_protocol_rejects_unrelated_object():
    """反例断言：防止 Protocol 退化成"空 Protocol"（空 Protocol 对一切返回 True）。"""
    mod = load_pg_storage()

    class Unrelated:
        def not_connect(self):
            return None

    assert not isinstance(Unrelated(), mod.VoiceStoreProtocol)


def test_postgres_store_satisfies_protocol_but_is_not_sqlite_subclass():
    mod = load_pg_storage()
    store, _ = make_store()
    assert isinstance(store, mod.VoiceStoreProtocol)
    # 不许靠继承 SQLite VoiceStore 来骗过 control_plane_base.py:42 的 isinstance 硬绑定
    assert not issubclass(mod.PostgresVoiceStore, VoiceStore)


# --------------------------------------------------------------------------
# B. 注入式构造（不硬依赖全局单例 / 不依赖 psycopg 已安装）
# --------------------------------------------------------------------------


def test_construction_accepts_dsn_and_injected_pool_factory():
    store, pool = make_store()
    assert store is not None
    assert pool.configs, "pool_factory 应收到 PostgresPoolConfig"
    assert getattr(pool.configs[0], "dsn", None) == VALID_DSN


def test_injected_pool_factory_never_imports_psycopg():
    before = set(sys.modules)
    store, _ = make_store()
    with store.connect() as conn:
        conn.execute("SELECT 1")
    with store.transaction() as conn:
        conn.execute("SELECT 1")
    leaked = {"psycopg", "psycopg_pool"} & (set(sys.modules) - before)
    assert not leaked, f"注入式 pool 下仍导入了 psycopg: {leaked}"


def test_construction_accepts_injected_pool_instance():
    mod = load_pg_storage()
    pool = FakePool()
    store = mod.PostgresVoiceStore(dsn=VALID_DSN, pool=pool)
    with store.connect() as conn:
        conn.execute("SELECT 1")
    assert "SELECT 1" in pool.sql


# --------------------------------------------------------------------------
# C. PostgresPoolConfig：必须显式带超时
# --------------------------------------------------------------------------


def test_pool_config_requires_explicit_connect_timeout():
    mod = load_pg_storage()
    with pytest.raises(TypeError):
        mod.PostgresPoolConfig(dsn=VALID_DSN, min_size=1, max_size=5, max_idle_s=30.0)


def test_pool_config_requires_explicit_idle_or_lifetime_bound():
    mod = load_pg_storage()
    with pytest.raises(TypeError):
        mod.PostgresPoolConfig(dsn=VALID_DSN, min_size=1, max_size=5, connect_timeout_s=3.0)


def test_pool_config_rejects_invalid_size_window():
    mod = load_pg_storage()
    with pytest.raises(mod.PostgresPoolConfigError):
        mod.PostgresPoolConfig(
            dsn=VALID_DSN, min_size=10, max_size=2,
            connect_timeout_s=3.0, max_idle_s=30.0,
        )


def test_pool_config_rejects_non_positive_timeout():
    mod = load_pg_storage()
    with pytest.raises(mod.PostgresPoolConfigError):
        mod.PostgresPoolConfig(
            dsn=VALID_DSN, min_size=1, max_size=5,
            connect_timeout_s=0, max_idle_s=30.0,
        )


def test_pool_config_rejects_zero_min_size():
    mod = load_pg_storage()
    with pytest.raises(mod.PostgresPoolConfigError):
        mod.PostgresPoolConfig(
            dsn=VALID_DSN, min_size=0, max_size=5,
            connect_timeout_s=3.0, max_idle_s=30.0,
        )


# --------------------------------------------------------------------------
# D. 事务语义：PG 的 BEGIN，绝不能是 SQLite 的 BEGIN IMMEDIATE
# --------------------------------------------------------------------------


def test_transaction_emits_begin_and_never_begin_immediate():
    store, pool = make_store()
    with store.transaction() as conn:
        conn.execute("SELECT 1")
    upper = [s.upper() for s in pool.sql]
    assert any("BEGIN" in s for s in upper), f"未发送 BEGIN，实际 SQL: {pool.sql}"
    assert not any("BEGIN IMMEDIATE" in s for s in upper), f"出现 SQLite 专有语句: {pool.sql}"


def test_transaction_commits_on_success():
    store, pool = make_store()
    with store.transaction() as conn:
        conn.execute("INSERT INTO session_events (session_id) VALUES ('s1')")
    assert "COMMIT" in [s.upper() for s in pool.sql]


def test_transaction_rolls_back_on_error():
    store, pool = make_store()
    with pytest.raises(RuntimeError):
        with store.transaction():
            raise RuntimeError("boom")
    upper = [s.upper() for s in pool.sql]
    assert "ROLLBACK" in upper
    assert "COMMIT" not in upper


def test_transaction_never_emits_sqlite_pragma():
    store, pool = make_store()
    with store.transaction() as conn:
        conn.execute("SELECT 1")
    assert not any("PRAGMA" in s.upper() for s in pool.sql), f"出现 SQLite PRAGMA: {pool.sql}"


def _code_without_docstrings(src: str) -> str:
    """剥掉模块/类/函数 docstring，只留可执行代码做静态扫描。

    注释与 docstring 里允许出现 `BEGIN IMMEDIATE` 字样（说明"为什么不能用"），
    可执行代码里绝不允许。
    """
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_pg_storage_source_contains_no_sqlite_begin_immediate():
    mod = load_pg_storage()
    src = Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
    code = _code_without_docstrings(src)
    assert not re.search(r"BEGIN\s+IMMEDIATE", code, re.IGNORECASE)
    assert not re.search(r"journal_mode|busy_timeout", code, re.IGNORECASE)


# --------------------------------------------------------------------------
# E. 连接 lease 归还 / 池优雅释放
# --------------------------------------------------------------------------


def test_connect_leases_and_returns_connection():
    store, pool = make_store()
    with store.connect() as conn:
        assert pool.opened == 1
        assert pool.returned == 0
        conn.execute("SELECT 1")
    assert pool.returned == 1, "连接未归还，存在 lease 泄漏"
    assert "SELECT 1" in pool.sql


def test_connect_yields_dict_row_factory():
    store, pool = make_store()
    with store.connect() as conn:
        assert conn.row_factory is not None, "PG 侧必须设置 dict_row，否则 repository 拿不到映射"


def test_close_releases_pool_exactly_once():
    store, pool = make_store()
    store.close()
    assert pool.close_calls == 1
    store.close()
    assert pool.close_calls == 1, "close() 必须幂等"


def test_aclose_releases_pool_exactly_once():
    store, pool = make_store()
    asyncio.run(store.aclose())
    assert pool.close_calls == 1


# --------------------------------------------------------------------------
# F. DSN fail-closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_dsn", ["sqlite:///tmp/x.db", "mysql://u:p@h/db", "", "postgrest://h/db"])
def test_non_postgres_dsn_fails_closed(bad_dsn):
    mod = load_pg_storage()
    with pytest.raises(mod.InvalidPostgresDsnError):
        mod.PostgresVoiceStore(dsn=bad_dsn, pool_factory=lambda cfg: FakePool())


@pytest.mark.parametrize("ok_dsn", [VALID_DSN, VALID_DSN_ALT])
def test_postgres_dsn_prefixes_accepted(ok_dsn):
    store, _ = make_store(dsn=ok_dsn)
    assert store is not None


# --------------------------------------------------------------------------
# H. 池生命周期：open_pool 必须真正打开池
# --------------------------------------------------------------------------


class _OpenablePool:
    """可观察 open/closed 的池替身（模拟 psycopg_pool.ConnectionPool 的语义）。"""

    def __init__(self, *, closed: bool = True, accept_kwargs: bool = True) -> None:
        self.closed = closed
        self.accept_kwargs = accept_kwargs
        self.open_calls: list[dict] = []
        self.no_arg_calls = 0

    def open(self, *args, **kwargs) -> None:
        if kwargs and not self.accept_kwargs:
            raise TypeError("open() takes no keyword arguments")
        if kwargs:
            self.open_calls.append(kwargs)
        else:
            self.no_arg_calls += 1
        self.closed = False

    @contextmanager
    def connection(self):
        yield FakeConnection(self)


def test_open_pool_actually_opens_a_closed_pool():
    """生产事故回归：池以 open=False 构造后从未 open，首个请求即 PoolClosed。

    云端实测表现：任何用到存储的端点都在 0.22s 内 500（不是网络超时——池本身
    从未打开，`pool.connection()` 立即抛错）。
    """
    mod = load_pg_storage()
    pool = _OpenablePool(closed=True)
    store = mod.PostgresVoiceStore(dsn=VALID_DSN, pool=pool)

    store.open_pool()

    assert pool.open_calls, "open_pool() 必须真正调用 pool.open()，不能只构造池"
    assert pool.open_calls[0]["wait"] is True, "必须等待建连，数据库不可达时启动即失败"
    assert pool.open_calls[0]["timeout"] == store.config.connect_timeout_s
    assert pool.closed is False


def test_open_pool_is_idempotent_for_an_already_open_pool():
    mod = load_pg_storage()
    pool = _OpenablePool(closed=False)
    store = mod.PostgresVoiceStore(dsn=VALID_DSN, pool=pool)

    store.open_pool()

    assert pool.open_calls == [] and pool.no_arg_calls == 0


def test_open_pool_tolerates_pools_without_open_or_closed():
    """注入的替身没有 open/closed 属性时不得报错（离线契约测试依赖此行为）。"""
    store, fake = make_store()
    store.open_pool()
    assert fake.opened == 0


def test_open_pool_falls_back_to_no_argument_open():
    mod = load_pg_storage()
    pool = _OpenablePool(closed=True, accept_kwargs=False)
    store = mod.PostgresVoiceStore(dsn=VALID_DSN, pool=pool)

    store.open_pool()

    assert pool.no_arg_calls == 1
