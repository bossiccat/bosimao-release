"""VoiceStore：商业双工语音 SQLite 安全存储门面（SPEC §6 / §9.1）

- 单文件真实 SQLite，正式迁移 001_commercial_voice.sql（含 schema_migrations 版本表）
- 显式事务（sqlite3 上下文管理器）、WAL 并发、外键开启
- Secret/pairing_code/nonce 一律只存哈希，审计 metadata 走脱敏白名单

与 PostgreSQL 路径（`pg_storage.PostgresVoiceStore`）共享同一批 repository 实现与
同一份门面逻辑（`store_facade.VoiceStoreFacade`），差异只在 `SQLITE_DIALECT`。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .migration_runner import apply_migrations, split_sql_script
from .repositories import audit as _audit
from .repositories import device_credentials as _dc
from .repositories import nonces as _nonces
from .repositories import pairing_codes as _pc
from .repositories import pending_sessions as _pending
from .repositories import hello_proofs as _hello_proofs
from .repositories import rate_limit as _rl
from .repositories import settings as _settings
from .sql_dialect import SQLITE_DIALECT
from .store_facade import DeviceRow, VoiceStoreFacade

AuditRepository = _audit.AuditRepository
DeviceCredentialRepository = _dc.DeviceCredentialRepository
DeviceCredentialRow = _dc.DeviceCredentialRow
NonceRepository = _nonces.NonceRepository
PairingCodeRepository = _pc.PairingCodeRepository
PendingSessionRepository = _pending.PendingSessionRepository
HelloProofRepository = _hello_proofs.HelloProofRepository
RateLimitRepository = _rl.RateLimitRepository
SettingsRepository = _settings.SettingsRepository

__all__ = [
    "DeviceRow",
    "VoiceStore",
    "AuditRepository",
    "DeviceCredentialRepository",
    "DeviceCredentialRow",
    "NonceRepository",
    "PairingCodeRepository",
    "PendingSessionRepository",
    "HelloProofRepository",
    "RateLimitRepository",
    "SettingsRepository",
]


def _open_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # 2026-08-13 高压 H1 发现：WAL 下默认 FULL 每事务 fsync，200 并发签发 30.1s。
    # NORMAL 是 SQLite 官方对 WAL 的推荐：崩溃不损坏数据库，仅可能丢失最近提交
    # （voice session 签发可重试，可接受），显著提升写入吞吐。
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class VoiceStore(VoiceStoreFacade):
    """SQLite 存储门面：初始化迁移 + 各资源仓库"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.dialect = SQLITE_DIALECT
        # connect 是上下文管理器工厂：退出即关闭连接，不允许透传或缓存。
        connect = self.connect
        self.pairing_codes = PairingCodeRepository(connect, SQLITE_DIALECT)
        self.pending_sessions = PendingSessionRepository(connect, SQLITE_DIALECT)
        self.hello_proofs = HelloProofRepository(connect, SQLITE_DIALECT)
        self.device_credentials = DeviceCredentialRepository(connect, SQLITE_DIALECT)
        self.nonces = NonceRepository(connect, SQLITE_DIALECT)
        self.rate_limit = RateLimitRepository(connect, SQLITE_DIALECT)
        self.audit = AuditRepository(connect, SQLITE_DIALECT)
        self.settings = SettingsRepository(connect, SQLITE_DIALECT)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """借出一个连接；lease 必须由调用方的 with 块归还。

        返回类型与 `PostgresVoiceStore.connect` 保持一致（`Iterator[Any]`）：
        两者都是**上下文管理器工厂**，门面签名必须可对齐校验。
        """
        conn = _open_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    _split_sql_script = staticmethod(split_sql_script)

    def initialize(self) -> None:
        with self.connect() as conn:
            apply_migrations(conn)
