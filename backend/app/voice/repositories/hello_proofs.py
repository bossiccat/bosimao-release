"""Persistence for hash-only commercial hello proof records.

同一份实现服务 SQLite 与 PostgreSQL：事务、占位符、时间列、json/jsonb 全部走
`self.dialect`（PG 侧由 psycopg 的 `Connection.transaction()` 发 BEGIN）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from .base import RepositoryBase


class HelloProofConflict(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("hello proof state rejected")
        self.code = code


class HelloProofRepository(RepositoryBase):
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
        ts = self.dialect.timestamp_to_storage(time.time() if now is None else now)
        with self._txn() as conn:
            session = conn.execute(
                "SELECT device_id, room_id, generation, state FROM control_plane_sessions"
                f" WHERE session_id = {self.ph}",
                (claims["session_id"],),
            ).fetchone()
            expected = (
                claims["device_id"], claims["room_id"], claims["generation"], "SIGNING"
            )
            if session is None or self._identity(session) != expected:
                raise HelloProofConflict(40914)
            claim = conn.execute(
                "UPDATE pending_session_claims SET signed_at ="
                f" {self.ph}, updated_at = {self.ph}"
                f" WHERE session_id = {self.ph} AND device_id = {self.ph}"
                f" AND generation = {self.ph} AND claim_token_hash = {self.ph}"
                " AND signed_at IS NULL",
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
                f" VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph},"
                f" {self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph},"
                f" {self.ph}, {self.ph}, {self.ph}, NULL, {self.ph}, {self.ph})",
                (
                    claims["jti"], self.digest(claims["nonce"]), self.digest(proof),
                    claims["session_id"], claims["device_id"], claims["room_id"],
                    claims["sidecar_user_id"], claims["generation"], claims["kid"],
                    claims["iss"], claims["aud"], claims["protocol_version"],
                    self.dialect.json_to_storage(
                        claims["audio_format"], sort_keys=True, separators=(",", ":")
                    ),
                    self.dialect.timestamp_to_storage(claims["iat"]),
                    self.dialect.timestamp_to_storage(claims["exp"]), ts, ts,
                ),
            )
            updated = conn.execute(
                "UPDATE control_plane_sessions SET state = 'ENTERING', updated_at ="
                f" {self.ph} WHERE session_id = {self.ph} AND generation = {self.ph}"
                " AND state = 'SIGNING'",
                (ts, claims["session_id"], claims["generation"]),
            )
            if updated.rowcount != 1:
                raise HelloProofConflict(40914)

    def redeem(
        self,
        claims: Mapping[str, Any],
        proof: str,
        body: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        import time

        ts = self.dialect.timestamp_to_storage(time.time() if now is None else now)
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

        with self._txn() as conn:
            row = conn.execute(
                f"SELECT * FROM control_plane_hello_proofs WHERE jti = {self.ph}",
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
            }
            if any(row[key] != value for key, value in expected.items()):
                raise HelloProofConflict(40914)
            # json/jsonb 列在两边形态不同（TEXT vs dict），统一反解成 Python 对象再比。
            if self.dialect.json_from_storage(
                row["audio_format_json"]
            ) != self._canonical_audio(claims["audio_format"]):
                raise HelloProofConflict(40914)
            session = conn.execute(
                "SELECT device_id, room_id, generation, state FROM control_plane_sessions"
                f" WHERE session_id = {self.ph}",
                (claims["session_id"],),
            ).fetchone()
            if session is None or self._identity(session) != (
                claims["device_id"], claims["room_id"], claims["generation"], "ENTERING"
            ):
                raise HelloProofConflict(40914)
            consumed = conn.execute(
                "UPDATE control_plane_hello_proofs SET consumed_at ="
                f" {self.ph}, updated_at = {self.ph} WHERE jti = {self.ph}"
                " AND consumed_at IS NULL",
                (ts, ts, claims["jti"]),
            )
            if consumed.rowcount != 1:
                raise HelloProofConflict(40113)
            activated = conn.execute(
                "UPDATE control_plane_sessions SET state = 'ACTIVE', updated_at ="
                f" {self.ph} WHERE session_id = {self.ph} AND generation = {self.ph}"
                " AND state = 'ENTERING'",
                (ts, claims["session_id"], claims["generation"]),
            )
            if activated.rowcount != 1:
                raise HelloProofConflict(40914)
        return {
            "redeemed": True,
            **{key: claims[key] for key in identity[:5]},
            "expires_at": claims["exp"],
        }

    @staticmethod
    def _canonical_audio(audio_format: Any) -> Any:
        """audio_format 的规范化形态：与方言无关的 dict（排序键 + 紧凑分隔符）。"""
        return json.loads(
            json.dumps(audio_format, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _identity(row: Any) -> tuple:
        """按列名取 (device_id, room_id, generation, state)。

        `tuple(row)` 在 sqlite3.Row 上是值、在 dict_row 上是键，必须显式取值。
        """
        return (row["device_id"], row["room_id"], row["generation"], row["state"])
