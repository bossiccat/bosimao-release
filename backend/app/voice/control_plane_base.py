"""Shared constants, errors and transaction helpers for the control-plane ledger."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .pg_storage import VoiceStoreProtocol

ACKNOWLEDGEMENTS = (
    "android_trtc_left", "sidecar_trtc_left", "bridge_drained_closed",
    "apm_cancelled_closed", "brain_turns_sealed",
)
ACK_REPORTERS = {
    "android_trtc_left": frozenset({"android"}),
    "sidecar_trtc_left": frozenset({"sidecar"}),
    "bridge_drained_closed": frozenset({"sidecar", "rtc_bridge"}),
    "apm_cancelled_closed": frozenset({"rtc_bridge"}),
    "brain_turns_sealed": frozenset({"brain"}),
}
ROOT_TERMINATE_ALLOWED_STATES = frozenset({"ACTIVE"})
RETRY_ALLOWED_STATES = frozenset({"TERMINATION_PARTIAL", "TERMINATION_TIMEOUT"})
RETRY_REASON_BY_PARENT_RESULT = {
    "partial": "retry_failed_acknowledgements", "timeout": "retry_timeout",
}


class IdempotencyConflict(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = 40912


class InvalidTerminationState(Exception):
    def __init__(self, message: str, code: int = 40916) -> None:
        super().__init__(message)
        self.code = code


class LedgerBase:
    def __init__(self, store: VoiceStoreProtocol, *, user_sig_cipher: Any = None) -> None:
        # 结构性校验（runtime_checkable Protocol）：SQLite VoiceStore 与
        # PostgresVoiceStore 都满足，靠继承造假的无关对象仍然被拒。
        if not isinstance(store, VoiceStoreProtocol):
            raise TypeError("SessionLedger requires an existing VoiceStore")
        self.store = store
        # userSig 静态加密器必须显式注入（密钥来自 KMS/Secret/env）。None 时
        # consume_wake() 一律 fail-closed，绝不退化成明文落库。
        self.user_sig_cipher = user_sig_cipher

    def initialize(self) -> None:
        self.store.initialize()

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _begin(conn: Any) -> None:
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _finish(conn: Any, error: BaseException | None = None) -> None:
        conn.rollback() if error is not None else conn.commit()

    @staticmethod
    def _termination_context(conn: Any, termination_id: str) -> Any:
        row = conn.execute(
            "SELECT t.*, s.device_id, s.room_id, s.state AS session_state"
            " FROM control_plane_terminations t"
            " JOIN control_plane_sessions s ON s.session_id = t.session_id"
            " WHERE t.termination_id = ?", (termination_id,),
        ).fetchone()
        if row is None:
            raise InvalidTerminationState("termination not found", code=40403)
        return row
