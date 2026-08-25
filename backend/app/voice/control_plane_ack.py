"""Acknowledgement and termination completion ledger operations."""
from __future__ import annotations

import time
from typing import Any

from .control_plane_base import ACKNOWLEDGEMENTS, ACK_REPORTERS, InvalidTerminationState


class AcknowledgementLedgerMixin:
    @staticmethod
    def _aggregate_ack_result(conn: Any, termination_id: str,
                              acknowledgement: str) -> str:
        required = ACK_REPORTERS[acknowledgement]
        reports = conn.execute(
            "SELECT reporter, result FROM control_plane_ack_reports"
            " WHERE termination_id = ? AND acknowledgement = ?",
            (termination_id, acknowledgement),
        ).fetchall()
        latest = {row["reporter"]: row["result"] for row in reports}
        if any(value == "failed" for value in latest.values()):
            return "failed"
        if all(latest.get(reporter) == "confirmed" for reporter in required):
            return "confirmed"
        return "pending"

    def record_ack(self, *, termination_id: str, session_id: str, device_id: str,
                   room_id: str, generation: int, acknowledgement: str,
                   reporter: str, result: str,
                   error_code: str | None = None) -> dict[str, Any]:
        if acknowledgement not in ACK_REPORTERS:
            raise InvalidTerminationState("unknown acknowledgement", code=40901)
        if reporter not in ACK_REPORTERS[acknowledgement]:
            raise InvalidTerminationState("reporter is not authorized", code=40901)
        if result not in {"confirmed", "failed"}:
            raise InvalidTerminationState("invalid acknowledgement result", code=40901)
        now = time.time()
        with self.store.connect() as conn:
            self._begin(conn)
            try:
                termination = self._termination_context(conn, termination_id)
                context = (termination["session_id"], termination["device_id"],
                           termination["room_id"], termination["generation"])
                if context != (session_id, device_id, room_id, generation):
                    raise InvalidTerminationState("acknowledgement context mismatch", code=40901)
                if termination["result"] != "pending":
                    raise InvalidTerminationState("termination is already terminal", code=40901)
                current = conn.execute(
                    "SELECT result, inherited FROM control_plane_acknowledgements"
                    " WHERE termination_id = ? AND acknowledgement = ?",
                    (termination_id, acknowledgement),
                ).fetchone()
                if current is not None and current["inherited"]:
                    raise InvalidTerminationState(
                        "inherited acknowledgement is read-only", code=40901
                    )
                conn.execute(
                    "INSERT INTO control_plane_ack_reports"
                    " (termination_id, acknowledgement, reporter, result, error_code,"
                    " reported_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(termination_id, acknowledgement, reporter) DO UPDATE SET"
                    " result = excluded.result, error_code = excluded.error_code,"
                    " reported_at = excluded.reported_at, updated_at = excluded.updated_at",
                    (termination_id, acknowledgement, reporter, result, error_code,
                     now, now, now),
                )
                aggregate = self._aggregate_ack_result(conn, termination_id, acknowledgement)
                if current is None:
                    conn.execute(
                        "INSERT INTO control_plane_acknowledgements"
                        " (termination_id, acknowledgement, result, inherited, created_at, updated_at)"
                        " VALUES (?, ?, ?, 0, ?, ?)",
                        (termination_id, acknowledgement, aggregate, now, now),
                    )
                else:
                    conn.execute(
                        "UPDATE control_plane_acknowledgements SET result = ?, updated_at = ?"
                        " WHERE termination_id = ? AND acknowledgement = ?",
                        (aggregate, now, termination_id, acknowledgement),
                    )
                confirmed = conn.execute(
                    "SELECT COUNT(*) FROM control_plane_acknowledgements"
                    " WHERE termination_id = ? AND result = 'confirmed'", (termination_id,),
                ).fetchone()[0]
                if confirmed == len(ACKNOWLEDGEMENTS):
                    conn.execute(
                        "UPDATE control_plane_terminations SET result = 'complete',"
                        " state = 'TERMINATED', terminal_at = ?, updated_at = ?"
                        " WHERE termination_id = ?", (now, now, termination_id),
                    )
                    conn.execute(
                        "UPDATE control_plane_sessions SET state = 'TERMINATED', updated_at = ?"
                        " WHERE session_id = ? AND generation = ?",
                        (now, session_id, generation),
                    )
                self._finish(conn)
            except BaseException as exc:
                self._finish(conn, exc)
                raise
        return self.get_termination(termination_id)

    def finish_termination(self, termination_id: str, *, result: str) -> dict[str, Any]:
        if result not in {"partial", "timeout"}:
            raise InvalidTerminationState("invalid termination result")
        state = "TERMINATION_PARTIAL" if result == "partial" else "TERMINATION_TIMEOUT"
        now = time.time()
        with self.store.connect() as conn:
            self._begin(conn)
            try:
                termination = self._termination_context(conn, termination_id)
                if termination["result"] != "pending":
                    raise InvalidTerminationState("termination is already terminal")
                conn.execute(
                    "UPDATE control_plane_terminations SET result = ?, state = ?,"
                    " terminal_at = ?, updated_at = ? WHERE termination_id = ?",
                    (result, state, now, now, termination_id),
                )
                conn.execute(
                    "UPDATE control_plane_sessions SET state = ?, updated_at = ?"
                    " WHERE session_id = ?", (state, now, termination["session_id"]),
                )
                self._finish(conn)
            except BaseException as exc:
                self._finish(conn, exc)
                raise
        return self.get_termination(termination_id)
