"""VoiceStore：商业双工语音 SQLite 安全存储门面（SPEC §6 / §9.1）

- 单文件真实 SQLite，正式迁移 001_commercial_voice.sql（含 schema_migrations 版本表）
- 显式事务（sqlite3 上下文管理器）、WAL 并发、外键开启
- Secret/pairing_code/nonce 一律只存哈希，审计 metadata 走脱敏白名单
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .._frozen_paths import bundled_path
from .repositories import audit as _audit
from .repositories import device_credentials as _dc
from .repositories import nonces as _nonces
from .repositories import pairing_codes as _pc
from .repositories import pending_sessions as _pending
from .repositories import rate_limit as _rl
from .repositories import settings as _settings
from .repositories.common import now_unix

AuditRepository = _audit.AuditRepository
DeviceCredentialRepository = _dc.DeviceCredentialRepository
DeviceCredentialRow = _dc.DeviceCredentialRow
NonceRepository = _nonces.NonceRepository
PairingCodeRepository = _pc.PairingCodeRepository
PendingSessionRepository = _pending.PendingSessionRepository
RateLimitRepository = _rl.RateLimitRepository
SettingsRepository = _settings.SettingsRepository

MIGRATIONS_DIR = bundled_path("backend", "app", "voice", "migrations")
MIGRATIONS = (
    "001_commercial_voice.sql",
    "002_pending_session_claims.sql",
    "003_credential_identity.sql",
    "004_pending_claim_tokens.sql",
)


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


@dataclass
class DeviceRow:
    device_id: str
    credential_hash: str
    status: str
    expires_at: float
    revoked_at: float | None


class VoiceStore:
    """SQLite 存储门面：初始化迁移 + 各资源仓库"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        connect = lambda: _open_connection(self.db_path)  # noqa: E731
        self.pairing_codes = PairingCodeRepository(connect)
        self.pending_sessions = PendingSessionRepository(connect)
        self.device_credentials = DeviceCredentialRepository(connect)
        self.nonces = NonceRepository(connect)
        self.rate_limit = RateLimitRepository(connect)
        self.audit = AuditRepository(connect)
        self.settings = SettingsRepository(connect)

    @contextmanager
    def connect(self) -> sqlite3.Connection:
        conn = _open_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """执行未应用的迁移；schema_migrations 记录版本，幂等可重入"""
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                row[0] for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for migration in MIGRATIONS:
                if migration in applied:
                    continue
                script = (MIGRATIONS_DIR / migration).read_text(encoding="utf-8")
                with conn:
                    conn.executescript(script)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at)"
                        " VALUES (?, strftime('%s','now'))",
                        (migration,),
                    )

    # ---- device credentials（Secret 只哈希） ----

    def save_device(self, device_id: str, secret: str, device_name: str = "phone",
                    platform: str = "android", expires_at: float | None = None,
                    now: float | None = None, credential_id: str | None = None) -> None:
        import time as _time

        expiry = expires_at or _time.time() + 30 * 86400
        stable_id = credential_id or f"cred-{device_id}"
        self.device_credentials.save(
            device_id, stable_id, device_name, platform, secret, expiry, now=now
        )

    def get_device(self, device_id: str) -> DeviceRow | None:
        row = self.device_credentials.get(device_id)
        if row is None:
            return None
        return DeviceRow(
            device_id=row.device_id,
            credential_hash=row.credential_hash,
            status=row.status,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    def verify_device_secret(self, device_id: str, secret: str) -> DeviceCredentialRow | None:
        return self.device_credentials.verify(device_id, secret)

    def revoke_device(self, device_id: str, reason: str, now: float | None = None) -> bool:
        return self.device_credentials.revoke(device_id, reason, now=now)

    def list_devices(self) -> list[DeviceCredentialRow]:
        return self.device_credentials.list_active()

    # ---- pairing code（明文只返回一次，库中只存哈希） ----

    def create_pairing_code(self, owner_id: str, platform: str, ttl_seconds: int,
                            now: float | None = None) -> tuple[str, dict]:
        return self.pairing_codes.create(owner_id, platform, ttl_seconds, now=now)

    def consume_pairing_code(self, code: str, device_id: str, now: float | None = None) -> bool:
        return self.pairing_codes.consume(code, device_id, now=now)

    def register_device_from_pairing(self, pairing_code: str, device_id: str,
                                     credential_id: str, device_name: str, platform: str,
                                     secret: str, expires_at: float,
                                     now: float | None = None) -> bool:
        """原子注册：同一事务内消费 pairing_code + 创建设备凭证 + 写审计。

        只存 code_hash 与 credential_hash；配对码已消费/过期时返回 False，不创建半成品。
        """
        import json as _json

        from .repositories.device_credentials import hash_secret
        from .repositories.pairing_codes import hash_code

        ts = now_unix(now)
        credential_hash = hash_secret(secret)
        with self.connect() as conn:
            with conn:
                cursor = conn.execute(
                    "UPDATE pairing_codes SET consumed_at = ?, consumed_device_id = ?,"
                    " updated_at = ? WHERE code_hash = ? AND consumed_at IS NULL"
                    " AND expires_at > ?",
                    (ts, device_id, ts, hash_code(pairing_code), ts),
                )
                if cursor.rowcount != 1:
                    return False
                conn.execute(
                    "INSERT INTO device_credentials"
                    " (device_id, credential_id, device_name, platform, credential_hash, status,"
                    "  expires_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (device_id, credential_id, device_name, platform, credential_hash,
                     expires_at, ts, ts),
                )
                conn.execute(
                    "INSERT INTO privacy_audit_events"
                    " (action, subject_type, subject_id, result, metadata_redacted_json,"
                    "  created_at, updated_at)"
                    " VALUES ('device.register', 'device', ?, 'ok', ?, ?, ?)",
                    (device_id, _json.dumps({"platform": platform}), ts, ts),
                )
        return True

    def record_revoke_confirmation(self, device_id: str, reason: str,
                                   sessions: list[dict],
                                   now: float | None = None) -> dict | None:
        """强一致撤销事务：credential 立即失效 + 登记未过期 userSig 指纹 + 终止事件 + 审计。

        sessions: [{session_id, fingerprint, expires_at}]（调用方已过滤未过期）。
        返回 {device_id, revoked_at, terminated_session_ids}；设备不存在返回 None。
        幂等：已 revoked 直接返回当前状态，不重复登记。
        """
        import json as _json

        ts = now_unix(now)
        with self.connect() as conn:
            with conn:
                row = conn.execute(
                    "SELECT status, revoked_at FROM device_credentials WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if row is None:
                    return None
                if row["status"] != "revoked":
                    return None
                terminated: list[str] = []
                for session in sessions:
                    conn.execute(
                        "INSERT INTO revoked_sessions"
                        " (session_id, device_id, user_sig_fingerprint, expires_at,"
                        "  revoked_at, reason, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (session["session_id"], device_id, session["fingerprint"],
                         session["expires_at"], ts, reason, ts, ts),
                    )
                    conn.execute(
                        "INSERT INTO session_events"
                        " (session_id, device_id, event_type, state, metadata_json,"
                        "  created_at, updated_at)"
                        " VALUES (?, ?, 'terminated', 'IDLE', '{}', ?, ?)",
                        (session["session_id"], device_id, ts, ts),
                    )
                    terminated.append(session["session_id"])
                conn.execute(
                    "INSERT INTO privacy_audit_events"
                    " (action, subject_type, subject_id, result, metadata_redacted_json,"
                    "  created_at, updated_at)"
                    " VALUES ('device.revoke', 'device', ?, 'ok', ?, ?, ?)",
                    (device_id, _json.dumps({"reason_len": len(reason),
                                             "terminated": len(terminated)}), ts, ts),
                )
        return {"device_id": device_id, "revoked_at": ts, "terminated_session_ids": terminated}

    # ---- nonce（主体绑定 + 哈希 + 原子消费） ----

    def consume_nonce(self, subject_id: str, nonce: str, ttl_seconds: int = 300,
                      now: float | None = None) -> bool:
        return self.nonces.consume(subject_id, nonce, ttl_seconds=ttl_seconds, now=now)

    def purge_expired_nonces(self, now: float | None = None) -> int:
        return self.nonces.purge_expired(now=now)

    # ---- 脱敏审计 ----

    def write_audit(self, action: str, subject_type: str, subject_id: str, result: str,
                    metadata_redacted_json: dict, now: float | None = None) -> None:
        self.audit.write(action, subject_type, subject_id, result, metadata_redacted_json, now=now)

    # ---- settings（隐私开关等键值对） ----

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str, now: float | None = None) -> None:
        self.settings.set(key, value, now=now)

    # ---- session_events（状态可观测；metadata 同样走脱敏校验） ----

    def write_session_event(self, session_id: str, device_id: str, event_type: str,
                            state: str | None = None, error_code: str | None = None,
                            metadata: dict | None = None, now: float | None = None) -> None:
        import json as _json
        from .repositories.common import assert_redacted_json

        meta = metadata or {}
        assert_redacted_json(meta)
        ts = now_unix(now)
        with self.connect() as conn:
            with conn:  # 显式事务：self.connect 是 contextmanager，自身不提交
                conn.execute(
                    "INSERT INTO session_events"
                    " (session_id, device_id, event_type, state, error_code,"
                    "  metadata_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, device_id, event_type, state, error_code,
                     _json.dumps(meta, ensure_ascii=False), ts, ts),
                )

    def list_session_events(self, device_id: str, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, device_id, event_type, state, error_code,"
                " metadata_json, created_at FROM session_events"
                " WHERE device_id = ? ORDER BY created_at DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- pending session control plane（metadata only，原子单次领取） ----

    def enqueue_pending_session(self, session_id: str, device_id: str, room_id: str,
                                expires_at: float, now: float | None = None) -> None:
        self.pending_sessions.enqueue(session_id, device_id, room_id, expires_at, now=now)

    def claim_pending_session(self, now: float | None = None) -> dict | None:
        return self.pending_sessions.claim_one(now=now)

    def consume_pending_sign_claim(self, session_id: str, device_id: str,
                                   claim_token: str,
                                   now: float | None = None) -> dict | None:
        return self.pending_sessions.consume_sign_claim(
            session_id, device_id, claim_token, now=now
        )
