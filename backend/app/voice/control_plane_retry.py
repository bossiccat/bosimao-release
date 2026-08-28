"""Retry and termination read ledger operations."""
from __future__ import annotations

import time
import uuid
from typing import Any

from .control_plane_base import (
    ACKNOWLEDGEMENTS, IdempotencyConflict, InvalidTerminationState,
    RETRY_ALLOWED_STATES, RETRY_REASON_BY_PARENT_RESULT,
)


class RetryLedgerMixin:
    def retry_termination(self, *, session_id: str, parent_termination_id: str,
                          request_id: str, reason: str) -> dict[str, Any]:
        now = time.time()
        with self.store.connect() as conn:
            self._begin(conn)
            try:
                parent = self._termination_context(conn, parent_termination_id)
                if parent["session_id"] != session_id:
                    raise InvalidTerminationState("retry session mismatch")
                if parent["result"] not in {"partial", "timeout"}:
                    raise InvalidTerminationState("termination not retryable", code=40913)
                if parent["session_state"] not in RETRY_ALLOWED_STATES:
                    raise InvalidTerminationState("session state does not allow retry", code=40913)
                if reason != RETRY_REASON_BY_PARENT_RESULT.get(parent["result"]):
                    raise InvalidTerminationState("reason is invalid for parent", code=40913)
                child = conn.execute(
                    "SELECT result FROM control_plane_terminations"
                    " WHERE parent_termination_id = ?", (parent_termination_id,),
                ).fetchone()
                if child is not None:
                    raise InvalidTerminationState("parent already has a child", code=40913)
                payload = {
                    "session_id": session_id, "device_id": parent["device_id"],
                    "room_id": parent["room_id"], "generation": parent["generation"],
                    "reason": reason, "parent_termination_id": parent_termination_id,
                }
                payload_hash = self._payload_hash(payload)
                existing = conn.execute(
                    "SELECT termination_id, payload_hash FROM control_plane_terminations"
                    " WHERE session_id = ? AND generation = ? AND request_id = ?",
                    (session_id, parent["generation"], request_id),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise IdempotencyConflict("idempotency key payload mismatch")
                    termination_id = existing["termination_id"]
                else:
                    termination_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO control_plane_terminations"
                        " (termination_id, session_id, generation, operation, request_id,"
                        " payload_hash, parent_termination_id, result, state, terminal_at,"
                        " created_at, updated_at) VALUES (?, ?, ?, 'retry', ?, ?, ?,"
                        " 'pending', 'TERMINATING', NULL, ?, ?)",
                        (termination_id, session_id, parent["generation"], request_id,
                         payload_hash, parent_termination_id, now, now),
                    )
                    conn.execute(
                        "INSERT INTO control_plane_acknowledgements"
                        " (termination_id, acknowledgement, result, inherited, created_at, updated_at)"
                        " SELECT ?, acknowledgement, result, 1, ?, ?"
                        " FROM control_plane_acknowledgements"
                        " WHERE termination_id = ? AND result = 'confirmed'",
                        (termination_id, now, now, parent_termination_id),
                    )
                    conn.execute(
                        "UPDATE control_plane_sessions SET state = 'TERMINATING', updated_at = ?"
                        " WHERE session_id = ?", (now, session_id),
                    )
                self._finish(conn)
            except BaseException as exc:
                self._finish(conn, exc)
                raise
        return self.get_termination(termination_id)

    def get_termination(self, termination_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT termination_id, session_id, generation, operation, request_id,"
                " payload_hash, parent_termination_id, result, state, terminal_at"
                " FROM control_plane_terminations WHERE termination_id = ?",
                (termination_id,),
            ).fetchone()
            if row is None:
                raise InvalidTerminationState("termination not found", code=40403)
            acknowledgements = dict.fromkeys(ACKNOWLEDGEMENTS, "pending")
            for ack in conn.execute(
                "SELECT acknowledgement, result FROM control_plane_acknowledgements"
                " WHERE termination_id = ?", (termination_id,),
            ).fetchall():
                acknowledgements[ack["acknowledgement"]] = ack["result"]
        result = dict(row)
        result["acknowledgements"] = acknowledgements
        return result

    def count_terminations(self, session_id: str) -> int:
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM control_plane_terminations WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
