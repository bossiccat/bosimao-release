"""VOICE_PRODUCTION=true 时 PostgreSQL 装配的 RED 契约测试（TDD）。

目的：云端 jax-voice-api 起不来的唯一代码侧阻塞是入口层的 fail-closed raise
（`backend/app/main.py:88-93` 与 `cloudapi/main.py:89-93`）。本契约把"生产必须
装配出 PostgresVoiceStore、且绝不静默回退 SQLite"这件事钉死：

- 生产 + 合法 PG DSN → `PostgresVoiceStore`（**不是** `VoiceStore` 的子类/实例）
- 生产 + 空/非法 DSN → fail-closed 抛 `ProductionGateError`
- 生产 + psycopg 缺失 → 抛 `PsycopgNotAvailableError`，**不回退 SQLite**
- 非生产 → 仍是初始化过的 SQLite `VoiceStore`（开发/测试夹具，行为不变）
- 连接池参数必须真的传到池里（用注入的 pool_factory 断值，不数 mock 调用次数）
- `SessionLedger` 接受 `PostgresVoiceStore`，同时仍拒绝无关对象

不连真实数据库、不依赖 psycopg 是否安装：池通过 `pool_factory` 注入。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

# backend/tests/contract 下没有 __init__.py，pytest 不会自动把 backend/ 放进 sys.path，
# 与 test_voice_pg_adapter_contract.py / test_cloudrun_canonical_source.py 的做法一致。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.voice.config import ProductionGateError  # noqa: E402
from app.voice.control_plane import SessionLedger  # noqa: E402
from app.voice.pg_storage import (  # noqa: E402
    PostgresPoolConfig,
    PostgresVoiceStore,
    PostgresVoiceStoreError,
    PsycopgNotAvailableError,
    VoiceStoreProtocol,
)
from app.voice import storage as voice_storage  # noqa: E402
from app.voice.storage import VoiceStore  # noqa: E402

FACTORY_MODULE = "app.voice.store_factory"
DSN = "postgresql://jax:secret@db.internal:5432/jax_voice"


def load_factory():
    """惰性导入被测装配模块：RED 阶段这里抛 ModuleNotFoundError。"""
    return importlib.import_module(FACTORY_MODULE)


def production_settings(**overrides) -> Settings:
    values = {
        "voice_production": True,
        "voice_storage_backend": "postgresql",
        "voice_database_url": DSN,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# 假池：记录真实收到的 PostgresPoolConfig，而不是"被调用了几次"
# ---------------------------------------------------------------------------


class FakePool:
    def __init__(self, config: PostgresPoolConfig) -> None:
        self.config = config
        self.closed = False

    def close(self) -> None:
        self.closed = True


class RecordingPoolFactory:
    def __init__(self) -> None:
        self.configs: list[PostgresPoolConfig] = []
        self.pools: list[FakePool] = []

    def __call__(self, config: PostgresPoolConfig) -> FakePool:
        self.configs.append(config)
        pool = FakePool(config)
        self.pools.append(pool)
        return pool


# ---------------------------------------------------------------------------
# A. 生产路径：装配出 PostgreSQL store，绝不回退 SQLite
# ---------------------------------------------------------------------------


def test_production_wires_postgres_store_and_never_sqlite() -> None:
    factory = load_factory()
    recorder = RecordingPoolFactory()

    store = factory.build_voice_store(production_settings(), pool_factory=recorder)

    assert isinstance(store, PostgresVoiceStore)
    # 不许靠继承 SQLite VoiceStore 骗过老的 isinstance 硬绑定
    assert not issubclass(PostgresVoiceStore, VoiceStore)
    assert not isinstance(store, VoiceStore)
    assert isinstance(store, VoiceStoreProtocol)
    assert store.dsn == DSN


def test_production_rejects_missing_or_non_postgres_dsn() -> None:
    factory = load_factory()
    for bad_url in ("", "   ", "sqlite:///tmp/voice.db", "mysql://u:p@h/db"):
        recorder = RecordingPoolFactory()
        with pytest.raises(ProductionGateError):
            factory.build_voice_store(
                production_settings(voice_database_url=bad_url), pool_factory=recorder
            )
        # fail-closed 必须发生在建池之前：一次都没走到池
        assert recorder.configs == []


def test_production_rejects_sqlite_backend_declaration() -> None:
    factory = load_factory()
    recorder = RecordingPoolFactory()
    with pytest.raises(ProductionGateError):
        factory.build_voice_store(
            production_settings(voice_storage_backend="sqlite"), pool_factory=recorder
        )
    assert recorder.configs == []


def test_production_fails_closed_when_psycopg_is_missing(monkeypatch) -> None:
    """psycopg 缺失即抛 PsycopgNotAvailableError，绝不静默回退 SQLite。"""
    factory = load_factory()
    # `from psycopg_pool import ConnectionPool` → ImportError
    monkeypatch.setitem(sys.modules, "psycopg_pool", None)

    constructed: list[tuple] = []

    class ExplodingVoiceStore:
        def __init__(self, *args, **kwargs) -> None:
            constructed.append((args, kwargs))
            raise AssertionError("production path must never build a SQLite VoiceStore")

    monkeypatch.setattr(voice_storage, "VoiceStore", ExplodingVoiceStore)

    with pytest.raises(PsycopgNotAvailableError):
        factory.build_voice_store(production_settings())

    assert constructed == [], "生产路径回退到了 SQLite VoiceStore"


# ---------------------------------------------------------------------------
# B. 连接池参数真的传下去了
# ---------------------------------------------------------------------------


def test_pool_settings_are_propagated_to_the_pool() -> None:
    factory = load_factory()
    recorder = RecordingPoolFactory()

    store = factory.build_voice_store(
        production_settings(
            voice_pg_pool_min_size=3,
            voice_pg_pool_max_size=9,
            voice_pg_connect_timeout_s=2.5,
            voice_pg_max_idle_s=90.0,
        ),
        pool_factory=recorder,
    )

    assert len(recorder.configs) == 1
    config = recorder.configs[0]
    assert config.dsn == DSN
    assert config.min_size == 3
    assert config.max_size == 9
    assert config.connect_timeout_s == 2.5
    assert config.max_idle_s == 90.0
    # 装配期就把池建出来（psycopg 缺失要在此 fail-closed，而不是首次请求才炸）
    assert store.open_pool() is recorder.pools[0]


def test_production_rejects_zero_pool_settings() -> None:
    factory = load_factory()
    recorder = RecordingPoolFactory()
    with pytest.raises(ProductionGateError):
        factory.build_voice_store(
            production_settings(voice_pg_pool_min_size=0), pool_factory=recorder
        )
    assert recorder.configs == []


# ---------------------------------------------------------------------------
# C. 开发路径不变 + schema 归属 + 优雅关闭
# ---------------------------------------------------------------------------


def test_development_path_still_builds_initialized_sqlite_store(tmp_path) -> None:
    factory = load_factory()
    db_path = tmp_path / "voice.db"

    store = factory.build_voice_store(
        Settings(voice_production=False, voice_db_path=str(db_path))
    )

    assert isinstance(store, VoiceStore)
    assert not isinstance(store, PostgresVoiceStore)
    # 真实语义：initialize() 已跑过，迁移表确实存在（不是"方法被调用过"）
    with store.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"pairing_codes", "consumed_nonces", "device_credentials"} <= tables


def test_production_assembly_does_not_run_initialize() -> None:
    """schema 由 CloudBase migration 拥有，代码侧不做隐式 DDL。"""
    factory = load_factory()
    recorder = RecordingPoolFactory()

    # 装配成功即证明没有调用 initialize()——stage 1 的 initialize 必然 raise
    store = factory.build_voice_store(production_settings(), pool_factory=recorder)

    with pytest.raises(PostgresVoiceStoreError):
        store.initialize()


def test_shutdown_closes_the_production_pool() -> None:
    factory = load_factory()
    recorder = RecordingPoolFactory()
    store = factory.build_voice_store(production_settings(), pool_factory=recorder)
    pool = recorder.pools[0]

    assert pool.closed is False
    asyncio.run(factory.shutdown_voice_store(store))
    assert pool.closed is True


# ---------------------------------------------------------------------------
# D. ledger 的 protocol 校验
# ---------------------------------------------------------------------------


def test_session_ledger_accepts_postgres_store() -> None:
    factory = load_factory()
    store = factory.build_voice_store(
        production_settings(), pool_factory=RecordingPoolFactory()
    )

    ledger = SessionLedger(store)
    assert ledger.store is store


def test_session_ledger_still_rejects_unrelated_objects() -> None:
    with pytest.raises(TypeError):
        SessionLedger(object())
    with pytest.raises(TypeError):
        SessionLedger("not-a-store")
