"""Atomic pending-session discovery and one-time sign authorization claims."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Callable

from .common import now_unix


class PendingSessionRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def enqueue(self, session_id: str, device_id: str, room_id: str,
                expires_at: float, now: float | None = None) -> None:
        ts = now_unix(now)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_session_claims"
                " (session_id, device_id, room_id, expires_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, device_id, room_id, expires_at, ts, ts),
            )

    def claim_one(self, now: float | None = None) -> dict | None:
        """Discover one intent and mint a bearer claim exactly once."""
        ts = now_unix(now)
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, session_id, device_id, room_id, expires_at"
                " FROM pending_session_claims"
                " WHERE claimed_at IS NULL AND expires_at > ?"
                " ORDER BY created_at ASC, id ASC LIMIT 1",
                (ts,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                "UPDATE pending_session_claims"
                " SET claim_token_hash = ?, claimed_at = ?, consumed_at = ?, updated_at = ?"
                " WHERE id = ? AND claimed_at IS NULL",
                (token_hash, ts, ts, ts, row["id"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
        return {
            "session_id": row["session_id"],
            "device_id": row["device_id"],
            "room_id": row["room_id"],
            "expires_at": row["expires_at"],
            "claim_token": token,
        }

    def consume_sign_claim(self, session_id: str, device_id: str, claim_token: str,
                           now: float | None = None) -> dict | None:
        """Atomically consume a claim if token/session/device/expiry/device status all match."""
        ts = now_unix(now)
        token_hash = self._hash_token(claim_token)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT p.id, p.session_id, p.device_id, p.room_id, p.expires_at"
                " FROM pending_session_claims p"
                " JOIN device_credentials d ON d.device_id = p.device_id"
                " WHERE p.session_id = ? AND p.device_id = ? AND p.claim_token_hash = ?"
                " AND p.claimed_at IS NOT NULL AND p.signed_at IS NULL AND p.expires_at > ?"
                " AND d.status = 'active' AND d.revoked_at IS NULL AND d.expires_at > ?",
                (session_id, device_id, token_hash, ts, ts),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            updated = conn.execute(
                "UPDATE pending_session_claims SET signed_at = ?, updated_at = ?"
                " WHERE id = ? AND signed_at IS NULL",
                (ts, ts, row["id"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
        return dict(row)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
