"""语音存储装配（fail-closed）：生产走 PostgreSQL，开发/测试走 SQLite 夹具。

两条路径的语义：

- ``voice_production=True`` → `PostgresVoiceStore`。DSN 必须是 ``postgresql://``
  （由 `validate_voice_storage` 与 `PostgresPoolConfig` 双重校验），池参数全部
  显式传入，psycopg 缺失即在**装配期**抛 `PsycopgNotAvailableError`。
  **绝不构造 SQLite，绝不静默回退。**
- ``voice_production=False`` → SQLite `VoiceStore`（开发/测试夹具），行为与
  接线前完全一致（构造 + `initialize()`）。

schema 归属：PostgreSQL 的 DDL 只由 `cloudbase/migrations/` 拥有，代码侧不做
隐式建表，因此生产路径**不调用** `store.initialize()`。
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

from .config import ProductionGateError, validate_voice_storage
from .pg_storage import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_MAX_IDLE_S,
    DEFAULT_MAX_SIZE,
    DEFAULT_MIN_SIZE,
    PostgresPoolConfig,
    PostgresVoiceStore,
)

__all__ = [
    "build_postgres_pool_config",
    "build_voice_store",
    "shutdown_voice_store",
]

# (settings 字段, PostgresPoolConfig 字段, 缺省值, 强制类型)
_POOL_FIELDS: tuple[tuple[str, str, Any, Callable[[Any], Any]], ...] = (
    ("voice_pg_pool_min_size", "min_size", DEFAULT_MIN_SIZE, int),
    ("voice_pg_pool_max_size", "max_size", DEFAULT_MAX_SIZE, int),
    ("voice_pg_connect_timeout_s", "connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S, float),
    ("voice_pg_max_idle_s", "max_idle_s", DEFAULT_MAX_IDLE_S, float),
)


def _pool_value(settings: Any, field: str, default: Any, cast: Callable[[Any], Any]) -> Any:
    """读池参数：缺省取合理默认值，0/空/负值一律 fail-closed（不静默修正）。"""
    raw = getattr(settings, field, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = default
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        raise ProductionGateError(f"invalid {field}: {raw!r}") from exc
    if value <= 0:
        raise ProductionGateError(f"{field} must be > 0 in production, got {raw!r}")
    return value


def build_postgres_pool_config(settings: Any) -> PostgresPoolConfig:
    """从 settings 构造池配置；任何一项不合法都在启动期拒绝，不留半装配状态。"""
    dsn = (getattr(settings, "voice_database_url", "") or "").strip()
    if not dsn:
        raise ProductionGateError("production PostgreSQL database URL is missing")
    kwargs = {
        name: _pool_value(settings, field, default, cast)
        for field, name, default, cast in _POOL_FIELDS
    }
    return PostgresPoolConfig(dsn=dsn, **kwargs)


def build_voice_store(
    settings: Any,
    *,
    pool_factory: Callable[[PostgresPoolConfig], Any] | None = None,
    pool: Any | None = None,
    sqlite_factory: Callable[[], Any] | None = None,
) -> Any:
    """按 `settings.voice_production` 装配存储门面。

    `pool_factory` / `pool` 仅供离线契约测试注入（不连真实数据库）。
    `sqlite_factory` 让入口把开发夹具留在自己文件里；缺省时惰性导入 SQLite 版。
    """
    production = bool(getattr(settings, "voice_production", False))
    # 存储边界门禁：生产必须声明 postgresql 且给出私密 DSN，否则在此拒绝。
    validate_voice_storage(
        production=production,
        storage_backend=getattr(settings, "voice_storage_backend", "sqlite"),
        database_url=getattr(settings, "voice_database_url", ""),
    )

    if not production:
        if sqlite_factory is not None:
            return sqlite_factory()
        from .storage import VoiceStore  # 惰性：生产路径不加载 SQLite 依赖

        store = VoiceStore(Path(settings.voice_db_path))
        store.initialize()
        return store

    config = build_postgres_pool_config(settings)
    store = PostgresVoiceStore(
        dsn=config.dsn, config=config, pool_factory=pool_factory, pool=pool
    )
    # 装配期建池（open=False，不建连）：psycopg 缺失要在此 fail-closed。
    # 不调用 initialize()：schema 由 CloudBase migration 拥有，代码侧不做隐式 DDL。
    store.open_pool()
    return store


async def shutdown_voice_store(store: Any) -> None:
    """优雅释放：PG 走池的 aclose/close；SQLite 夹具（无 close）天然 no-op。"""
    aclose = getattr(store, "aclose", None)
    if callable(aclose):
        result = aclose()
        if inspect.isawaitable(result):
            await result
        return
    close = getattr(store, "close", None)
    if callable(close):
        close()
