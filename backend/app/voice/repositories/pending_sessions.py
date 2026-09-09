"""Atomic pending-session discovery and one-time sign authorization claims.

同一份实现服务 SQLite 与 PostgreSQL：事务开始语句由方言决定（`BEGIN IMMEDIATE`
vs psycopg 的 `Connection.transaction()`）。
"""
from __future__ import annotations

import hashlib
import secrets

from .base import RepositoryBase
from .common import now_unix

_COLUMNS = "id, session_id, device_id, room_id, generation, expires_at"


class _Rollback(Exception):
    """内部信号：需要回滚当前事务但不向调用方抛错（方言层负责 rollback）。"""


class PendingSessionRepository(RepositoryBase):
    def enqueue(self, session_id: str, device_id: str, room_id: str,
                generation: int, expires_at: float, now: float | None = None) -> None:
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        self._execute(
            "INSERT INTO pending_session_claims"
            " (session_id, device_id, room_id, generation, expires_at, created_at, updated_at)"
            f" VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph},"
            f" {self.ph}, {self.ph})",
            (session_id, device_id, room_id, generation,
             self.dialect.timestamp_to_storage(expires_at), ts, ts),
        )

    def claim_one(self, now: float | None = None) -> dict | None:
        """Discover one intent and mint a bearer claim exactly once."""
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        claimed: dict | None = None
        try:
            with self._txn() as conn:
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM pending_session_claims"
                    f" WHERE claimed_at IS NULL AND expires_at > {self.ph}"
                    f" ORDER BY created_at ASC, id ASC LIMIT 1",
                    (ts,),
                ).fetchone()
                if row is None:
                    return None
                updated = conn.execute(
                    "UPDATE pending_session_claims"
                    " SET claim_token_hash ="
                    f" {self.ph}, claimed_at = {self.ph}, consumed_at = {self.ph},"
                    f" updated_at = {self.ph} WHERE id = {self.ph} AND claimed_at IS NULL",
                    (token_hash, ts, ts, ts, row["id"]),
                )
                if updated.rowcount != 1:
                    raise _Rollback
                claimed = {
                    "session_id": row["session_id"],
                    "device_id": row["device_id"],
                    "room_id": row["room_id"],
                    "generation": row["generation"],
                    "expires_at": self.dialect.timestamp_from_storage(row["expires_at"]),
                    "claim_token": token,
                }
        except _Rollback:
            return None
        return claimed

    def get_signing_context(self, session_id: str, device_id: str, claim_token: str,
                            now: float | None = None) -> dict | None:
        """Read a still-valid claim bound to the authoritative CP SIGNING session."""
        ts = self.dialect.timestamp_to_storage(now_unix(now))
        token_hash = self._hash_token(claim_token)
        row = self._fetchone(
            "SELECT p.id, p.session_id, p.device_id, p.room_id, p.generation, p.expires_at"
            " FROM pending_session_claims p"
            " JOIN device_credentials d ON d.device_id = p.device_id"
            " JOIN control_plane_sessions s ON s.session_id = p.session_id"
            f" WHERE p.session_id = {self.ph} AND p.device_id = {self.ph}"
            f" AND p.claim_token_hash = {self.ph}"
            f" AND p.claimed_at IS NOT NULL AND p.signed_at IS NULL"
            f" AND p.expires_at > {self.ph}"
            " AND d.status = 'active' AND d.revoked_at IS NULL"
            f" AND d.expires_at > {self.ph}"
            " AND s.device_id = p.device_id AND s.room_id = p.room_id"
            " AND s.generation = p.generation AND s.state = 'SIGNING'",
            (session_id, device_id, token_hash, ts, ts),
        )
        if row is None:
            return None
        result = dict(row)
        result["claim_token_hash"] = token_hash
        return result

    def consume_sign_claim(self, session_id: str, device_id: str, claim_token: str,
                           now: float | None = None) -> dict | None:
        return self.get_signing_context(session_id, device_id, claim_token, now=now)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
