"""CloudBase PostgreSQL adapter for the voice control plane — stage 1.

本模块只做一件事：把 **PostgreSQL 存储门面的形状**钉死，供后续 repository
切片逐块对接。它刻意**不是** `VoiceStore` 的子类——继承会让
`control_plane_base.py:42` 的 `isinstance(store, VoiceStore)` 硬绑定"意外通过"，
从而掩盖真正的迁移工作量；正确做法是后续把那处改成 `VoiceStoreProtocol`。

约束（与 RED 契约测试 `backend/tests/contract/test_voice_pg_adapter_contract.py` 对齐）:

- 事务一律走 psycopg3 的 `Connection.transaction()`（libpq 协议层 BEGIN/COMMIT），
  **绝不出现 SQLite 专有的 `BEGIN IMMEDIATE`**。
- 连接池可注入（`pool=` / `pool_factory=`），离线环境（未安装 psycopg）也能验证语义。
- 池参数必须显式带超时，缺失即 fail。
- DSN 非 PostgreSQL 即抛错，不静默降级到 SQLite。
"""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from .repositories import audit as _audit
from .repositories import device_credentials as _dc
from .repositories import hello_proofs as _hello_proofs
from .repositories import nonces as _nonces
from .repositories import pairing_codes as _pc
from .repositories import pending_sessions as _pending
from .repositories import rate_limit as _rl
from .repositories import settings as _settings
from .sql_dialect import POSTGRES_DIALECT
from .store_facade import VoiceStoreFacade

__all__ = [
    "VoiceStoreProtocol",
    "PostgresPoolConfig",
    "PostgresVoiceStore",
    "PostgresVoiceStoreError",
    "InvalidPostgresDsnError",
    "PostgresPoolConfigError",
    "PsycopgNotAvailableError",
    "DEFAULT_MIN_SIZE",
    "DEFAULT_MAX_SIZE",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_MAX_IDLE_S",
]

# 未显式提供池参数时的兜底值；仍然全部显式传入，不使用库默认。
DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 10
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_MAX_IDLE_S = 300.0

_POSTGRES_DSN_PREFIXES = ("postgresql://", "postgres://")

_UNSET = object()


# ---------------------------------------------------------------------------
# 错误类型（fail-closed，名字即语义）
# ---------------------------------------------------------------------------


class PostgresVoiceStoreError(RuntimeError):
    """PostgreSQL 语音存储门面基类错误，一律 fail-closed，不静默降级。"""


class InvalidPostgresDsnError(PostgresVoiceStoreError):
    """DSN 不是 PostgreSQL 连接串。拒绝退化到 SQLite / 其它引擎。"""


class PostgresPoolConfigError(PostgresVoiceStoreError):
    """连接池参数不合法（含缺失 connect/idle 超时、窗口倒置）。"""


class PsycopgNotAvailableError(PostgresVoiceStoreError):
    """psycopg 3 / psycopg_pool 未安装，且调用方未注入 pool。"""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceStoreProtocol(Protocol):
    """存储门面的**完整**契约。

    之所以把 8 个仓库属性与全部直接方法都钉进 Protocol：装配接线
    （`control_plane_base.py`）用 `isinstance(store, VoiceStoreProtocol)` 做结构
    校验，若 Protocol 只留 `connect()`/`initialize()`，PG 侧漏掉
    `hello_proofs`、`pending_sessions` 这类属性也能"通过"，第一个请求才
    `AttributeError`。钉全了，漏加即启动期拒绝。

    `close()` / `aclose()` 是 PG 连接池独有的释放语义，SQLite 侧不存在，因此不进
    Protocol（否则 SQLite 侧结构子类型断言会自相矛盾，只能靠继承造假通过）。
    """

    def connect(self) -> Any:
        """借出一个连接；lease 必须由调用方的 with 块归还。"""
        ...

    def initialize(self) -> None:
        """把 schema 推进到可用状态。"""
        ...

    # ---- 8 个仓库（与 SQLite VoiceStore 同名） ----

    pairing_codes: Any
    pending_sessions: Any
    hello_proofs: Any
    device_credentials: Any
    nonces: Any
    rate_limit: Any
    audit: Any
    settings: Any

    # ---- 直接方法（与 SQLite VoiceStore 同名同签名） ----

    def save_device(self, device_id: str, secret: str, device_name: str = "phone",
                    platform: str = "android", expires_at: float | None = None,
                    now: float | None = None, credential_id: str | None = None) -> None:
        ...

    def get_device(self, device_id: str) -> Any:
        ...

    def verify_device_secret(self, device_id: str, secret: str) -> Any | None:
        ...

    def revoke_device(self, device_id: str, reason: str, now: float | None = None) -> bool:
        ...

    def list_devices(self) -> list[Any]:
        ...

    def create_pairing_code(self, owner_id: str, platform: str, ttl_seconds: int,
                            now: float | None = None) -> tuple[str, dict]:
        ...

    def consume_pairing_code(self, code: str, device_id: str,
                             now: float | None = None) -> bool:
        ...

    def register_device_from_pairing(self, pairing_code: str, device_id: str,
                                     credential_id: str, device_name: str, platform: str,
                                     secret: str, expires_at: float,
                                     now: float | None = None) -> bool:
        ...

    def record_revoke_confirmation(self, device_id: str, reason: str,
                                   sessions: list[dict],
                                   now: float | None = None) -> dict | None:
        ...

    def consume_nonce(self, subject_id: str, nonce: str, ttl_seconds: int = 300,
                      now: float | None = None) -> bool:
        ...

    def purge_expired_nonces(self, now: float | None = None) -> int:
        ...

    def write_audit(self, action: str, subject_type: str, subject_id: str, result: str,
                    metadata_redacted_json: dict, now: float | None = None) -> None:
        ...

    def get_setting(self, key: str) -> str | None:
        ...

    def set_setting(self, key: str, value: str, now: float | None = None) -> None:
        ...

    def write_session_event(self, session_id: str, device_id: str, event_type: str,
                            state: str | None = None, error_code: str | None = None,
                            metadata: dict | None = None, now: float | None = None) -> None:
        ...

    def list_session_events(self, device_id: str, limit: int = 50) -> list[dict]:
        ...

    def enqueue_pending_session(self, session_id: str, device_id: str, room_id: str,
                                generation: int, expires_at: float,
                                now: float | None = None) -> None:
        ...

    def claim_pending_session(self, now: float | None = None) -> dict | None:
        ...

    def consume_pending_sign_claim(self, session_id: str, device_id: str,
                                   claim_token: str,
                                   now: float | None = None) -> dict | None:
        ...


# ---------------------------------------------------------------------------
# 池配置
# ---------------------------------------------------------------------------


def _is_postgres_dsn(dsn: str) -> bool:
    return isinstance(dsn, str) and dsn.strip().lower().startswith(_POSTGRES_DSN_PREFIXES)


@dataclass(frozen=True)
class PostgresPoolConfig:
    """psycopg_pool 参数。**所有字段都无默认值**：超时必须显式声明。"""

    dsn: str
    min_size: int
    max_size: int
    connect_timeout_s: float
    max_idle_s: float

    def __post_init__(self) -> None:
        if not _is_postgres_dsn(self.dsn):
            raise InvalidPostgresDsnError(
                f"PostgresPoolConfig requires a postgresql:// DSN, got {self.dsn!r}"
            )
        if self.min_size < 1:
            raise PostgresPoolConfigError(f"min_size must be >= 1, got {self.min_size}")
        if self.max_size < self.min_size:
            raise PostgresPoolConfigError(
                f"max_size ({self.max_size}) must be >= min_size ({self.min_size})"
            )
        if self.connect_timeout_s <= 0:
            raise PostgresPoolConfigError(
                f"connect_timeout_s must be > 0, got {self.connect_timeout_s}"
            )
        if self.max_idle_s <= 0:
            raise PostgresPoolConfigError(f"max_idle_s must be > 0, got {self.max_idle_s}")


# ---------------------------------------------------------------------------
# row factory
# ---------------------------------------------------------------------------


class _DictRowUnavailable:
    """psycopg 缺失时的 row_factory 占位。

    真实 PG 路径不可能到达：默认 pool 工厂会先抛 `PsycopgNotAvailableError`。
    只有在注入 fake pool 的离线契约测试里才可见，用于保证 `row_factory` 非空。
    """

    def __repr__(self) -> str:
        return "<dict_row unavailable: psycopg is not installed>"


def _resolve_row_factory() -> Any:
    """惰性解析 psycopg3 的 `dict_row`（不安装 psycopg 时返回占位）。"""
    try:
        from psycopg.rows import dict_row
    except ImportError:
        return _DictRowUnavailable()
    return dict_row


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PostgresVoiceStore(VoiceStoreFacade):
    """PostgreSQL 存储门面。

    与 SQLite `VoiceStore` 共享同一批 repository 与同一份门面逻辑
    （`VoiceStoreFacade`），差异只在 `POSTGRES_DIALECT`（`%s` 占位符、
    `timestamptz` 值、`Connection.transaction()`、jsonb）。**刻意不继承 VoiceStore**：
    继承会让结构性校验意外通过，掩盖真实迁移工作量。
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_factory: Callable[[PostgresPoolConfig], Any] | None = None,
        pool: Any | None = None,
        config: PostgresPoolConfig | None = None,
    ) -> None:
        if not _is_postgres_dsn(dsn):
            raise InvalidPostgresDsnError(
                f"voice storage requires a postgresql:// DSN, got {dsn!r}"
            )
        self.dsn = dsn
        self.config = config or PostgresPoolConfig(
            dsn=dsn,
            min_size=DEFAULT_MIN_SIZE,
            max_size=DEFAULT_MAX_SIZE,
            connect_timeout_s=DEFAULT_CONNECT_TIMEOUT_S,
            max_idle_s=DEFAULT_MAX_IDLE_S,
        )
        self._pool_factory = pool_factory
        self._pool = pool
        self._row_factory: Any = _UNSET
        self._closed = False
        # 门面：与 SQLite VoiceStore 同名的 8 个仓库，走 PG 方言（同一份实现）。
        self.dialect = POSTGRES_DIALECT
        connect = self.connect
        self.pairing_codes = _pc.PairingCodeRepository(connect, POSTGRES_DIALECT)
        self.pending_sessions = _pending.PendingSessionRepository(
            connect, POSTGRES_DIALECT
        )
        self.hello_proofs = _hello_proofs.HelloProofRepository(connect, POSTGRES_DIALECT)
        self.device_credentials = _dc.DeviceCredentialRepository(
            connect, POSTGRES_DIALECT
        )
        self.nonces = _nonces.NonceRepository(connect, POSTGRES_DIALECT)
        self.rate_limit = _rl.RateLimitRepository(connect, POSTGRES_DIALECT)
        self.audit = _audit.AuditRepository(connect, POSTGRES_DIALECT)
        self.settings = _settings.SettingsRepository(connect, POSTGRES_DIALECT)
        # 注入了工厂就立即建池：调用方需要能在构造后立刻拿到配置与池引用。
        if self._pool is None and pool_factory is not None:
            self._pool = pool_factory(self.config)

    # ---- 连接与事务 ----

    def _ensure_pool(self) -> Any:
        if self._pool is None:
            self._pool = self._open_default_pool()
        return self._pool

    def open_pool(self) -> Any:
        """装配期就把池建出来（psycopg 缺失在此 fail-closed，而非首次请求才炸）。

        psycopg_pool 以 `open=False` 构造，不会在装配期真正建连；注入了
        `pool=` / `pool_factory=` 时直接返回既有池，无副作用。
        """
        return self._ensure_pool()

    def _open_default_pool(self) -> Any:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - 取决于部署环境
            raise PsycopgNotAvailableError(
                "psycopg_pool is required for the PostgreSQL voice adapter; "
                "install 'psycopg[binary,pool]' or inject a pool via pool="
            ) from exc
        # open=False：首次 connect() 才真正建连，避免 import/构造期副作用。
        return ConnectionPool(
            self.config.dsn,
            min_size=self.config.min_size,
            max_size=self.config.max_size,
            timeout=self.config.connect_timeout_s,
            max_idle=self.config.max_idle_s,
            open=False,
        )

    def _apply_row_factory(self, conn: Any) -> None:
        if self._row_factory is _UNSET:
            self._row_factory = _resolve_row_factory()
        conn.row_factory = self._row_factory

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """借出一个 PG 连接。

        lease 归还完全依赖 `with` 块：退出即把连接还给池，**不允许把连接
        透传出去或缓存**（那正是 SQLite 版 `_open_connection` 的老问题）。
        """
        with self._ensure_pool().connection() as conn:
            self._apply_row_factory(conn)
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """显式事务。

        用 psycopg3 的 `Connection.transaction()`，由 libpq 在协议层发
        BEGIN / COMMIT / ROLLBACK。这里**永远不手写 `BEGIN IMMEDIATE`**——那是
        SQLite 专有语法（`control_plane_base.py:58` 的存量写法，迁移切片要清掉）。
        """
        with self._ensure_pool().connection() as conn:
            self._apply_row_factory(conn)
            with conn.transaction():
                yield conn

    def initialize(self) -> None:
        """推进 schema。

        Stage 1 刻意不做任何隐式 DDL：本阶段禁止执行远端 migration，与其静默
        建表不如显式失败。后续切片对接
        `cloudbase/migrations/20260908154838_voice_control_plane.sql` 时再实现。
        """
        raise PostgresVoiceStoreError(
            "PostgreSQL schema bootstrap is not wired yet (stage 1); "
            "run the tracked migration via the deployment pipeline before enabling it"
        )

    # ---- 释放 ----

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            self._pool.close()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool = self._pool
        if pool is None:
            return
        maybe = getattr(pool, "aclose", None)
        if maybe is None:
            pool.close()
            return
        result = maybe()
        if inspect.isawaitable(result):
            await result
