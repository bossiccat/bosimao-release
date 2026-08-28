"""Atomic KWS wake consumption ledger operations."""
from __future__ import annotations

import json
import time
from typing import Any

from .control_plane_base import InvalidTerminationState
from .timefmt import as_epoch, epoch_to_iso8601


class WakeLedgerMixin:
    def consume_wake(self, *, session_id: str, device_id: str,
                     prior_session_id: str, prior_generation: int,
                     wake_event_id: str, user_sig: str, expires_at: float | int | str,
                     detected_at: str = "", kws_instance_id: str = "",
                     user_id: str = "") -> dict[str, Any]:
        payload = {
            "device_id": device_id, "prior_session_id": prior_session_id,
            "prior_generation": prior_generation, "wake_event_id": wake_event_id,
            "detected_at": detected_at, "kws_instance_id": kws_instance_id,
        }
        payload_hash = self._payload_hash(payload)
        now = time.time()
        with self.store.connect() as conn:
            self._begin(conn)
            try:
                replay = conn.execute(
                    "SELECT record_json, payload_hash FROM control_plane_wake_events"
                    " WHERE device_id = ? AND prior_session_id = ?"
                    " AND prior_generation = ? AND wake_event_id = ?",
                    (device_id, prior_session_id, prior_generation, wake_event_id),
                ).fetchone()
                if replay is not None:
                    if replay["payload_hash"] != payload_hash:
                        raise InvalidTerminationState(
                            "wake event replayed with different payload", code=40913
                        )
                    self._finish(conn)
                    return json.loads(replay["record_json"])
                prior = conn.execute(
                    "SELECT * FROM control_plane_sessions WHERE session_id = ?",
                    (prior_session_id,),
                ).fetchone()
                if prior is None:
                    raise InvalidTerminationState("prior session not found", code=40402)
                if prior["device_id"] != device_id or prior["generation"] != prior_generation:
                    raise InvalidTerminationState("prior session context mismatch", code=40917)
                if prior["state"] != "KWS_READY":
                    raise InvalidTerminationState("prior session is not KWS_READY", code=40917)
                active = conn.execute(
                    "SELECT COUNT(*) FROM control_plane_sessions WHERE device_id = ?"
                    " AND state IN ('ACTIVE', 'TERMINATING', 'TERMINATION_PARTIAL',"
                    " 'TERMINATION_TIMEOUT', 'ENTERING')", (device_id,),
                ).fetchone()[0]
                if active:
                    raise InvalidTerminationState(
                        "device already has an active session", code=40917
                    )
                generation = prior_generation + 1
                room_id = f"wake-{session_id}"
                conn.execute(
                    "UPDATE control_plane_sessions SET state = 'SIGNING', updated_at = ?"
                    " WHERE session_id = ? AND state = 'KWS_READY'", (now, prior_session_id),
                )
                conn.execute(
                    "INSERT INTO control_plane_sessions"
                    " (session_id, device_id, room_id, generation, state, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'SIGNING', ?, ?)",
                    (session_id, device_id, room_id, generation, now, now),
                )
                # 同事务入队 pending claim：sidecar 轮询 /session/pending 领取后
                # 才能走 /session/sign → hello proof → redeem 链。claim 生命周期
                # 与本代 user_sig 一致（wake 路由传入的 expires_at）。
                conn.execute(
                    "INSERT INTO pending_session_claims"
                    " (session_id, device_id, room_id, generation, expires_at,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, device_id, room_id, generation,
                     as_epoch(expires_at), now, now),
                )
                record = {
                    "wake_event_id": wake_event_id, "prior_session_id": prior_session_id,
                    "prior_generation": prior_generation, "session_id": session_id,
                    "device_id": device_id, "room_id": room_id, "user_id": user_id,
                    # OpenAPI WakeSessionData.expires_at 声明 date-time；统一按
                    # ISO8601 持久化，重放路径返回的也是同一 ISO 字符串
                    "user_sig": user_sig,
                    "expires_at": epoch_to_iso8601(as_epoch(expires_at)),
                    "generation": generation, "state": "SIGNING",
                }
                conn.execute(
                    "INSERT INTO control_plane_wake_events"
                    " (wake_event_id, device_id, prior_session_id, prior_generation,"
                    " new_session_id, new_generation, payload_hash, record_json,"
                    " consumed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (wake_event_id, device_id, prior_session_id, prior_generation,
                     session_id, generation, payload_hash,
                     json.dumps(record, ensure_ascii=False, sort_keys=True), now, now, now),
                )
                self._finish(conn)
            except BaseException as exc:
                self._finish(conn, exc)
                raise
        return record

    def replay_wake_payload_mismatch(self, *, device_id: str, prior_session_id: str,
                                     prior_generation: int, wake_event_id: str,
                                     payload: dict[str, Any]) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT payload_hash FROM control_plane_wake_events"
                " WHERE device_id = ? AND prior_session_id = ?"
                " AND prior_generation = ? AND wake_event_id = ?",
                (device_id, prior_session_id, prior_generation, wake_event_id),
            ).fetchone()
        return row is not None and row["payload_hash"] != self._payload_hash(payload)
