"""SQLite persistence for hash-only commercial hello proof records."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any


class HelloProofConflict(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("hello proof state rejected")
        self.code = code


class HelloProofRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    @staticmethod
    def digest(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def create_for_signing(
        self,
        claims: Mapping[str, Any],
        proof: str,
        *,
        claim_token_hash: str,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else now
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    "SELECT device_id, room_id, generation, state FROM control_plane_sessions"
                    " WHERE session_id = ?",
                    (claims["session_id"],),
                ).fetchone()
                expected = (
                    claims["device_id"], claims["room_id"], claims["generation"], "SIGNING"
                )
                if session is None or tuple(session) != expected:
                    raise HelloProofConflict(40914)
                claim = conn.execute(
                    "UPDATE pending_session_claims SET signed_at = ?, updated_at = ?"
                    " WHERE session_id = ? AND device_id = ? AND generation = ?"
                    " AND claim_token_hash = ? AND signed_at IS NULL",
                    (ts, ts, claims["session_id"], claims["device_id"],
                     claims["generation"], claim_token_hash),
                )
                if claim.rowcount != 1:
                    raise HelloProofConflict(40914)
                conn.execute(
                    "INSERT INTO control_plane_hello_proofs"
                    " (jti, nonce_hash, proof_hash, session_id, device_id, room_id,"
                    " sidecar_user_id, generation, kid, issuer, audience, protocol_version,"
                    " audio_format_json, issued_at, expires_at, consumed_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        claims["jti"], self.digest(claims["nonce"]), self.digest(proof),
                        claims["session_id"], claims["device_id"], claims["room_id"],
                        claims["sidecar_user_id"], claims["generation"], claims["kid"],
                        claims["iss"], claims["aud"], claims["protocol_version"],
                        json.dumps(claims["audio_format"], sort_keys=True, separators=(",", ":")),
                        claims["iat"], claims["exp"], ts, ts,
                    ),
                )
                updated = conn.execute(
                    "UPDATE control_plane_sessions SET state = 'ENTERING', updated_at = ?"
                    " WHERE session_id = ? AND generation = ? AND state = 'SIGNING'",
                    (ts, claims["session_id"], claims["generation"]),
                )
                if updated.rowcount != 1:
                    raise HelloProofConflict(40914)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def redeem(
        self,
        claims: Mapping[str, Any],
        proof: str,
        body: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        if claims["jti"] != body["jti"] or not hmac.compare_digest(
            claims["nonce"], body["nonce"]
        ):
            raise HelloProofConflict(40113)
        identity = (
            "session_id", "device_id", "room_id", "sidecar_user_id", "generation",
            "protocol_version", "audio_format",
        )
        if any(claims[key] != body[key] for key in identity):
            raise HelloProofConflict(40111)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM control_plane_hello_proofs WHERE jti = ?",
                    (claims["jti"],),
                ).fetchone()
                if row is None or row["consumed_at"] is not None:
                    raise HelloProofConflict(40113)
                if not hmac.compare_digest(row["nonce_hash"], self.digest(body["nonce"])):
                    raise HelloProofConflict(40113)
                if not hmac.compare_digest(row["proof_hash"], self.digest(proof)):
                    raise HelloProofConflict(40111)
                expected = {
                    "session_id": claims["session_id"], "device_id": claims["device_id"],
                    "room_id": claims["room_id"], "sidecar_user_id": claims["sidecar_user_id"],
                    "generation": claims["generation"], "kid": claims["kid"],
                    "issuer": claims["iss"], "audience": claims["aud"],
                    "protocol_version": claims["protocol_version"],
                    "audio_format_json": json.dumps(
                        claims["audio_format"], sort_keys=True, separators=(",", ":")
                    ),
                }
                if any(row[key] != value for key, value in expected.items()):
                    raise HelloProofConflict(40914)
                session = conn.execute(
                    "SELECT device_id, room_id, generation, state FROM control_plane_sessions"
                    " WHERE session_id = ?",
                    (claims["session_id"],),
                ).fetchone()
                if session is None or tuple(session) != (
                    claims["device_id"], claims["room_id"], claims["generation"], "ENTERING"
                ):
                    raise HelloProofConflict(40914)
                consumed = conn.execute(
                    "UPDATE control_plane_hello_proofs SET consumed_at = ?, updated_at = ?"
                    " WHERE jti = ? AND consumed_at IS NULL",
                    (ts, ts, claims["jti"]),
                )
                if consumed.rowcount != 1:
                    raise HelloProofConflict(40113)
                activated = conn.execute(
                    "UPDATE control_plane_sessions SET state = 'ACTIVE', updated_at = ?"
                    " WHERE session_id = ? AND generation = ? AND state = 'ENTERING'",
                    (ts, claims["session_id"], claims["generation"]),
                )
                if activated.rowcount != 1:
                    raise HelloProofConflict(40914)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {
            "redeemed": True,
            **{key: claims[key] for key in identity[:5]},
            "expires_at": claims["exp"],
        }
