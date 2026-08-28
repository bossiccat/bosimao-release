"""KWS readiness ledger operations."""
from __future__ import annotations

import json
import time
from typing import Any

from .control_plane_base import InvalidTerminationState


class KwsReadinessLedgerMixin:
    def can_enter_kws_ready(self, session_id: str, generation: int) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM control_plane_sessions s WHERE s.session_id = ?"
                " AND s.generation = ? AND s.state = 'TERMINATED'"
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
        now = time.time()
        evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        with self.store.connect() as conn:
            self._begin(conn)
            try:
                conn.execute(
                    "INSERT INTO control_plane_kws_readiness"
                    " (session_id, generation, reporter, evidence_json, recorded_at,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(session_id, generation, reporter) DO UPDATE SET"
                    " evidence_json = excluded.evidence_json,"
                    " recorded_at = excluded.recorded_at, updated_at = excluded.updated_at",
                    (session_id, generation, reporter, evidence_json, now, now, now),
                )
                cursor = conn.execute(
                    "UPDATE control_plane_sessions SET state = 'KWS_READY', updated_at = ?"
                    " WHERE session_id = ? AND generation = ? AND state = 'TERMINATED'",
                    (now, session_id, generation),
                )
                self._finish(conn)
            except BaseException as exc:
                self._finish(conn, exc)
                raise
        return cursor.rowcount == 1

    def get_kws_readiness(self, session_id: str,
                          generation: int) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, generation, reporter, evidence_json, recorded_at"
                " FROM control_plane_kws_readiness WHERE session_id = ? AND generation = ?"
                " ORDER BY recorded_at ASC", (session_id, generation),
            ).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["evidence"] = json.loads(entry.pop("evidence_json"))
            result.append(entry)
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, device_id, room_id, generation, state"
                " FROM control_plane_sessions ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]
