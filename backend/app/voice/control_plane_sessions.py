"""Session and root termination ledger operations."""
from __future__ import annotations

import time
import uuid
from typing import Any

from .control_plane_base import (
    IdempotencyConflict, InvalidTerminationState, ROOT_TERMINATE_ALLOWED_STATES,
)


class SessionRootLedgerMixin:
    def create_session(self, *, session_id: str, device_id: str, room_id: str,
                       generation: int, state: str = "ACTIVE") -> dict[str, Any]:
        if state not in {"ACTIVE", "SIGNING"}:
            raise InvalidTerminationState("invalid initial session state")
        ph = self.ph
        # created_at / updated_at 在云端是 timestamptz：必须过方言（SQLite → Unix
        # float，PG → aware datetime），裸 float 会被 PG 直接拒。
        now = self.dialect.timestamp_to_storage(time.time())
        with self._txn() as conn:
            row = conn.execute(
                f"SELECT * FROM control_plane_sessions WHERE session_id = {ph}",
                (session_id,),
            ).fetchone()
            if row is not None:
                if (row["device_id"], row["room_id"], row["generation"]) != (
                    device_id, room_id, generation,
                ):
                    raise InvalidTerminationState("session context mismatch")
            else:
                conn.execute(
                    "INSERT INTO control_plane_sessions"
                    " (session_id, device_id, room_id, generation, state, created_at, updated_at)"
                    f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                    (session_id, device_id, room_id, generation, state, now, now),
                )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT session_id, device_id, room_id, generation, state"
                f" FROM control_plane_sessions WHERE session_id = {self.ph}", (session_id,),
            ).fetchone()
        if row is None:
            raise InvalidTerminationState("session not found", code=40402)
        return dict(row)

    @staticmethod
    def _validate_payload(row: Any, generation: int, payload: dict[str, Any]) -> None:
        if generation != row["generation"]:
            raise InvalidTerminationState("generation mismatch")
        required = {
            "session_id": row["session_id"], "device_id": row["device_id"],
            "room_id": row["room_id"], "generation": row["generation"],
        }
        if any(payload.get(key) != value for key, value in required.items()):
            raise InvalidTerminationState("session/device/room/generation context mismatch")

    def begin_termination(self, *, session_id: str, generation: int, request_id: str,
                          payload: dict[str, Any]) -> dict[str, Any]:
        payload_hash = self._payload_hash(payload)
        ph = self.ph
        # created_at / updated_at 是云端 timestamptz，必须过方言转换。
        now = self.dialect.timestamp_to_storage(time.time())
        with self._txn() as conn:
            session = conn.execute(
                f"SELECT * FROM control_plane_sessions WHERE session_id = {ph}",
                (session_id,),
            ).fetchone()
            if session is None:
                raise InvalidTerminationState("session not found", code=40402)
            self._validate_payload(session, generation, payload)
            existing = conn.execute(
                "SELECT termination_id, payload_hash FROM control_plane_terminations"
                f" WHERE session_id = {ph} AND generation = {ph} AND request_id = {ph}",
                (session_id, generation, request_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict("idempotency key payload mismatch")
                termination_id = existing["termination_id"]
            else:
                if session["state"] not in ROOT_TERMINATE_ALLOWED_STATES:
                    raise InvalidTerminationState(
                        f"session state {session['state']} does not allow new root termination",
                        code=40916,
                    )
                termination_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO control_plane_terminations"
                    " (termination_id, session_id, generation, operation, request_id,"
                    " payload_hash, parent_termination_id, result, state, terminal_at,"
                    f" created_at, updated_at) VALUES ({ph}, {ph}, {ph}, 'terminate', {ph},"
                    f" {ph}, NULL, 'pending', 'TERMINATING', NULL, {ph}, {ph})",
                    (termination_id, session_id, generation, request_id,
                     payload_hash, now, now),
                )
                conn.execute(
                    "UPDATE control_plane_sessions SET state = 'TERMINATING',"
                    f" updated_at = {ph} WHERE session_id = {ph}", (now, session_id),
                )
        return self.get_termination(termination_id)
