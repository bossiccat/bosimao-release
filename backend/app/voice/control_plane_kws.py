"""KWS readiness ledger operations."""
from __future__ import annotations

import time
from typing import Any

from .control_plane_base import InvalidTerminationState


class KwsReadinessLedgerMixin:
    def can_enter_kws_ready(self, session_id: str, generation: int) -> bool:
        ph = self.ph
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM control_plane_sessions s"
                f" WHERE s.session_id = {ph} AND s.generation = {ph}"
                " AND s.state = 'TERMINATED'"
                " AND EXISTS (SELECT 1 FROM control_plane_terminations t"
                " WHERE t.session_id = s.session_id AND t.generation = s.generation"
                " AND t.result = 'complete' AND t.state = 'TERMINATED')",
                (session_id, generation),
            ).fetchone()
        return row is not None

    def mark_kws_ready(self, session_id: str, generation: int, *,
                       reporter: str = "android",
                       evidence: dict[str, Any] | None = None) -> bool:
        evidence = evidence or {}
        for key, value in evidence.items():
            if not isinstance(key, str) or not isinstance(
                value, (str, int, float, bool, type(None))
            ):
                raise InvalidTerminationState("evidence must contain scalar string-key values")
        if not self.can_enter_kws_ready(session_id, generation):
            return False
        ph = self.ph
        # recorded_at / created_at / updated_at 是云端 timestamptz；evidence_json 是 jsonb。
        now = self.dialect.timestamp_to_storage(time.time())
        evidence_json = self.dialect.json_to_storage(evidence, sort_keys=True)
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO control_plane_kws_readiness"
                " (session_id, generation, reporter, evidence_json, recorded_at,"
                " created_at, updated_at)"
                f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
                " ON CONFLICT(session_id, generation, reporter) DO UPDATE SET"
                " evidence_json = excluded.evidence_json,"
                " recorded_at = excluded.recorded_at, updated_at = excluded.updated_at",
                (session_id, generation, reporter, evidence_json, now, now, now),
            )
            cursor = conn.execute(
                "UPDATE control_plane_sessions SET state = 'KWS_READY',"
                f" updated_at = {ph}"
                f" WHERE session_id = {ph} AND generation = {ph} AND state = 'TERMINATED'",
                (now, session_id, generation),
            )
        return cursor.rowcount == 1

    def get_kws_readiness(self, session_id: str,
                          generation: int) -> list[dict[str, Any]]:
        ph = self.ph
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, generation, reporter, evidence_json, recorded_at"
                f" FROM control_plane_kws_readiness WHERE session_id = {ph}"
                f" AND generation = {ph}"
                " ORDER BY recorded_at ASC", (session_id, generation),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["evidence"] = self.dialect.json_from_storage(entry.pop("evidence_json"))
            entry["recorded_at"] = self.dialect.timestamp_from_storage(entry["recorded_at"])
            result.append(entry)
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, device_id, room_id, generation, state"
                " FROM control_plane_sessions ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]
