"""Compatibility facade for the persistent control-plane session ledger."""
from __future__ import annotations

from .control_plane_ack import AcknowledgementLedgerMixin
from .control_plane_base import (
    ACKNOWLEDGEMENTS, ACK_REPORTERS, IdempotencyConflict,
    InvalidTerminationState, LedgerBase,
)
from .control_plane_kws import KwsReadinessLedgerMixin
from .control_plane_retry import RetryLedgerMixin
from .control_plane_sessions import SessionRootLedgerMixin
from .control_plane_wake import WakeLedgerMixin
from .storage import VoiceStore


class SessionLedger(
    SessionRootLedgerMixin,
    AcknowledgementLedgerMixin,
    RetryLedgerMixin,
    KwsReadinessLedgerMixin,
    WakeLedgerMixin,
    LedgerBase,
):
    """Unified compatibility API composed from domain-specific ledger mixins."""


__all__ = [
    "ACKNOWLEDGEMENTS", "ACK_REPORTERS", "IdempotencyConflict",
    "InvalidTerminationState", "SessionLedger", "VoiceStore",
]
