"""SQLite / PostgreSQL 共用的语音存储门面逻辑。

**仓库逻辑只写一份**：`VoiceStore`（SQLite）与 `PostgresVoiceStore`（PG）都通过
继承本 mixin 拿到 8 个仓库属性与全部直接方法，两条路径的差异只体现在
`self.dialect` 上（占位符、时间列、事务、JSON）。

刻意**不是**完整基类：`connect()` / `initialize()` 由各自的 store 实现
（SQLite 走本地文件 + migration_runner，PG 走连接池 + 云端 migration）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .repositories.common import assert_redacted_json, now_unix
from .sql_dialect import SqlDialect

__all__ = ["DeviceRow", "VoiceStoreFacade", "REPOSITORY_ATTRIBUTES"]

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


@dataclass
class DeviceRow:
    device_id: str
    credential_hash: str
    status: str
    expires_at: float
    revoked_at: float | None


class VoiceStoreFacade:
    """存储门面共享实现：只依赖 `self.dialect` 与各仓库，不依赖具体引擎。"""

    dialect: SqlDialect
    pairing_codes: Any
    pending_sessions: Any
    hello_proofs: Any
    device_credentials: Any
    nonces: Any
    rate_limit: Any
    audit: Any
    settings: Any

    # 由具体 store 提供；此处只声明形状（Protocol 的 isinstance 只看属性存在）。
    connect: Any
    initialize: Any

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

    def verify_device_secret(self, device_id: str, secret: str) -> Any | None:
        return self.device_credentials.verify(device_id, secret)

    def revoke_device(self, device_id: str, reason: str, now: float | None = None) -> bool:
        return self.device_credentials.revoke(device_id, reason, now=now)

    def list_devices(self) -> list[Any]:
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
        from .repositories.device_credentials import hash_secret
        from .repositories.pairing_codes import hash_code

        ph = self.dialect.placeholder
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        credential_hash = hash_secret(secret)
        with self.connect() as conn:
            with self.dialect.transaction(conn):
                cursor = conn.execute(
                    "UPDATE pairing_codes SET consumed_at ="
                    f" {ph}, consumed_device_id = {ph}, updated_at = {ph}"
                    f" WHERE code_hash = {ph} AND consumed_at IS NULL AND expires_at > {ph}",
                    (ts, device_id, ts, hash_code(pairing_code), ts),
                )
                if cursor.rowcount != 1:
                    return False
                conn.execute(
                    "INSERT INTO device_credentials"
                    " (device_id, credential_id, device_name, platform, credential_hash,"
                    "  status, expires_at, created_at, updated_at)"
                    f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, 'active', {ph}, {ph}, {ph})",
                    (device_id, credential_id, device_name, platform, credential_hash,
                     self.dialect.timestamp_to_storage(expires_at), ts, ts),
                )
                conn.execute(
                    "INSERT INTO privacy_audit_events"
                    " (action, subject_type, subject_id, result, metadata_redacted_json,"
                    "  created_at, updated_at)"
                    f" VALUES ('device.register', 'device', {ph}, 'ok', {ph}, {ph}, {ph})",
                    (device_id,
                     self.dialect.json_to_storage({"platform": platform}), ts, ts),
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
        ph = self.dialect.placeholder
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        with self.connect() as conn:
            with self.dialect.transaction(conn):
                row = conn.execute(
                    "SELECT status, revoked_at FROM device_credentials"
                    f" WHERE device_id = {ph}",
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
                        f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                        (session["session_id"], device_id, session["fingerprint"],
                         self.dialect.timestamp_to_storage(session["expires_at"]),
                         ts, reason, ts, ts),
                    )
                    conn.execute(
                        "INSERT INTO session_events"
                        " (session_id, device_id, event_type, state, metadata_json,"
                        "  created_at, updated_at)"
                        f" VALUES ({ph}, {ph}, 'terminated', 'IDLE', {ph}, {ph}, {ph})",
                        (session["session_id"], device_id,
                         self.dialect.json_to_storage({}), ts, ts),
                    )
                    terminated.append(session["session_id"])
                conn.execute(
                    "INSERT INTO privacy_audit_events"
                    " (action, subject_type, subject_id, result, metadata_redacted_json,"
                    "  created_at, updated_at)"
                    f" VALUES ('device.revoke', 'device', {ph}, 'ok', {ph}, {ph}, {ph})",
                    (device_id,
                     self.dialect.json_to_storage(
                         {"reason_len": len(reason), "terminated": len(terminated)}
                     ), ts, ts),
                )
        return {"device_id": device_id, "revoked_at": now_unix(now),
                "terminated_session_ids": terminated}

    # ---- nonce（主体绑定 + 哈希 + 原子消费） ----

    def consume_nonce(self, subject_id: str, nonce: str, ttl_seconds: int = 300,
                      now: float | None = None) -> bool:
        return self.nonces.consume(subject_id, nonce, ttl_seconds=ttl_seconds, now=now)

    def purge_expired_nonces(self, now: float | None = None) -> int:
        return self.nonces.purge_expired(now=now)

    # ---- 脱敏审计 ----

    def write_audit(self, action: str, subject_type: str, subject_id: str, result: str,
                    metadata_redacted_json: dict, now: float | None = None) -> None:
        self.audit.write(action, subject_type, subject_id, result,
                         metadata_redacted_json, now=now)

    # ---- settings（隐私开关等键值对） ----

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str, now: float | None = None) -> None:
        self.settings.set(key, value, now=now)

    # ---- session_events（状态可观测；metadata 同样走脱敏校验） ----

    def write_session_event(self, session_id: str, device_id: str, event_type: str,
                            state: str | None = None, error_code: str | None = None,
                            metadata: dict | None = None, now: float | None = None) -> None:
        meta = metadata or {}
        assert_redacted_json(meta)
        ph = self.dialect.placeholder
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        with self.connect() as conn:
            with self.dialect.transaction(conn):  # 显式事务：connect 自身不提交
                conn.execute(
                    "INSERT INTO session_events"
                    " (session_id, device_id, event_type, state, error_code,"
                    "  metadata_json, created_at, updated_at)"
                    f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                    (session_id, device_id, event_type, state, error_code,
                     self.dialect.json_to_storage(meta), ts, ts),
                )

    def list_session_events(self, device_id: str, limit: int = 50) -> list[dict]:
        ph = self.dialect.placeholder
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, device_id, event_type, state, error_code,"
                " metadata_json, created_at FROM session_events"
                f" WHERE device_id = {ph} ORDER BY created_at DESC LIMIT {ph}",
                (device_id, limit),
            ).fetchall()
        # created_at 归一成 Unix 秒（PG timestamptz 给的是 datetime），避免调用方分叉。
        events = []
        for row in rows:
            item = dict(row)
            item["created_at"] = self.dialect.timestamp_from_storage(item["created_at"])
            events.append(item)
        return events

    # ---- pending session control plane（metadata only，原子单次领取） ----

    def enqueue_pending_session(self, session_id: str, device_id: str, room_id: str,
                                generation: int, expires_at: float,
                                now: float | None = None) -> None:
        self.pending_sessions.enqueue(
            session_id, device_id, room_id, generation, expires_at, now=now
        )

    def claim_pending_session(self, now: float | None = None) -> dict | None:
        return self.pending_sessions.claim_one(now=now)

    def consume_pending_sign_claim(self, session_id: str, device_id: str,
                                   claim_token: str,
                                   now: float | None = None) -> dict | None:
        return self.pending_sessions.consume_sign_claim(
            session_id, device_id, claim_token, now=now
        )
