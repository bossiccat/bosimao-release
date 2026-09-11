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
        ph = self.ph
        # created_at / updated_at 是云端 timestamptz，必须过方言转换。
        now = self.dialect.timestamp_to_storage(time.time())
        with self._txn() as conn:
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
                f" WHERE parent_termination_id = {ph}", (parent_termination_id,),
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
                f" WHERE session_id = {ph} AND generation = {ph} AND request_id = {ph}",
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
                    " created_at, updated_at)"
                    f" VALUES ({ph}, {ph}, {ph}, 'retry', {ph}, {ph}, {ph},"
                    " 'pending', 'TERMINATING', NULL,"
                    f" {ph}, {ph})",
                    (termination_id, session_id, parent["generation"], request_id,
                     payload_hash, parent_termination_id, now, now),
                )
                conn.execute(
                    "INSERT INTO control_plane_acknowledgements"
                    " (termination_id, acknowledgement, result, inherited, created_at, updated_at)"
                    f" SELECT {ph}, acknowledgement, result, {ph}, {ph}, {ph}"
                    " FROM control_plane_acknowledgements"
                    f" WHERE termination_id = {ph} AND result = 'confirmed'",
                    # inherited=True 由参数绑定（boolean 列不接受整数 1 字面量）
                    (termination_id, True, now, now, parent_termination_id),
                )
                conn.execute(
                    "UPDATE control_plane_sessions SET state = 'TERMINATING',"
                    f" updated_at = {ph}"
                    f" WHERE session_id = {ph}", (now, session_id),
                )
        return self.get_termination(termination_id)

    def get_termination(self, termination_id: str) -> dict[str, Any]:
        ph = self.ph
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT termination_id, session_id, generation, operation, request_id,"
                " payload_hash, parent_termination_id, result, state, terminal_at"
                f" FROM control_plane_terminations WHERE termination_id = {ph}",
                (termination_id,),
            ).fetchone()
            if row is None:
                raise InvalidTerminationState("termination not found", code=40403)
            acknowledgements = dict.fromkeys(ACKNOWLEDGEMENTS, "pending")
            for ack in conn.execute(
                "SELECT acknowledgement, result FROM control_plane_acknowledgements"
                f" WHERE termination_id = {ph}", (termination_id,),
            ).fetchall():
                acknowledgements[ack["acknowledgement"]] = ack["result"]
        result = dict(row)
        # terminal_at 读出后归一成 Unix float，保证上层字段类型一致（SQLite/PG 一致）。
        result["terminal_at"] = self.dialect.timestamp_from_storage(result["terminal_at"])
        result["acknowledgements"] = acknowledgements
        return result

    def count_terminations(self, session_id: str) -> int:
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM control_plane_terminations"
                f" WHERE session_id = {self.ph}",
                (session_id,),
            ).fetchone()[0]
