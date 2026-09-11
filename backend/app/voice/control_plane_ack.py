"""Acknowledgement and termination completion ledger operations."""
from __future__ import annotations

import time
from typing import Any

from .control_plane_base import ACKNOWLEDGEMENTS, ACK_REPORTERS, InvalidTerminationState


class AcknowledgementLedgerMixin:
    def _aggregate_ack_result(self, conn: Any, termination_id: str,
                              acknowledgement: str) -> str:
        required = ACK_REPORTERS[acknowledgement]
        reports = conn.execute(
            "SELECT reporter, result FROM control_plane_ack_reports"
            f" WHERE termination_id = {self.ph} AND acknowledgement = {self.ph}",
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
        ph = self.ph
        # reported_at / created_at / updated_at / terminal_at 是云端 timestamptz，
        # 必须过方言（SQLite → Unix float，PG → aware datetime）。
        now = self.dialect.timestamp_to_storage(time.time())
        with self._txn() as conn:
            termination = self._termination_context(conn, termination_id)
            context = (termination["session_id"], termination["device_id"],
                       termination["room_id"], termination["generation"])
            if context != (session_id, device_id, room_id, generation):
                raise InvalidTerminationState("acknowledgement context mismatch", code=40901)
            if termination["result"] != "pending":
                raise InvalidTerminationState("termination is already terminal", code=40901)
            current = conn.execute(
                "SELECT result, inherited FROM control_plane_acknowledgements"
                f" WHERE termination_id = {ph} AND acknowledgement = {ph}",
                (termination_id, acknowledgement),
            ).fetchone()
            if current is not None and current["inherited"]:
                raise InvalidTerminationState(
                    "inherited acknowledgement is read-only", code=40901
                )
            conn.execute(
                "INSERT INTO control_plane_ack_reports"
                " (termination_id, acknowledgement, reporter, result, error_code,"
                " reported_at, created_at, updated_at)"
                f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
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
                    f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                    # inherited 是 boolean 列：必须绑定 Python 布尔，
                    # 写成整数字面量 0 在 PG 上会报
                    # "column inherited is of type boolean but expression is of type integer"。
                    (termination_id, acknowledgement, aggregate, False, now, now),
                )
            else:
                conn.execute(
                    f"UPDATE control_plane_acknowledgements SET result = {ph}, updated_at = {ph}"
                    f" WHERE termination_id = {ph} AND acknowledgement = {ph}",
                    (aggregate, now, termination_id, acknowledgement),
                )
            confirmed = conn.execute(
                "SELECT COUNT(*) FROM control_plane_acknowledgements"
                f" WHERE termination_id = {ph} AND result = 'confirmed'", (termination_id,),
            ).fetchone()[0]
            if confirmed == len(ACKNOWLEDGEMENTS):
                conn.execute(
                    "UPDATE control_plane_terminations SET result = 'complete',"
                    " state = 'TERMINATED',"
                    f" terminal_at = {ph}, updated_at = {ph}"
                    f" WHERE termination_id = {ph}", (now, now, termination_id),
                )
                conn.execute(
                    "UPDATE control_plane_sessions SET state = 'TERMINATED',"
                    f" updated_at = {ph}"
                    f" WHERE session_id = {ph} AND generation = {ph}",
                    (now, session_id, generation),
                )
        return self.get_termination(termination_id)

    def finish_termination(self, termination_id: str, *, result: str) -> dict[str, Any]:
        if result not in {"partial", "timeout"}:
            raise InvalidTerminationState("invalid termination result")
        state = "TERMINATION_PARTIAL" if result == "partial" else "TERMINATION_TIMEOUT"
        ph = self.ph
        now = self.dialect.timestamp_to_storage(time.time())
        with self._txn() as conn:
            termination = self._termination_context(conn, termination_id)
            if termination["result"] != "pending":
                raise InvalidTerminationState("termination is already terminal")
            conn.execute(
                f"UPDATE control_plane_terminations SET result = {ph}, state = {ph},"
                f" terminal_at = {ph}, updated_at = {ph} WHERE termination_id = {ph}",
                (result, state, now, now, termination_id),
            )
            conn.execute(
                f"UPDATE control_plane_sessions SET state = {ph}, updated_at = {ph}"
                f" WHERE session_id = {ph}",
                (state, now, termination["session_id"]),
            )
        return self.get_termination(termination_id)
