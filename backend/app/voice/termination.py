"""RTC session termination boundary for strong device revocation."""
from __future__ import annotations

from typing import Protocol


class RtcSessionTerminator(Protocol):
    """Terminate sessions and return only session ids confirmed out of the RTC room."""

    def terminate_and_wait(self, device_id: str, session_ids: list[str]) -> list[str]:
        ...


class UnavailableRtcSessionTerminator:
    """Fail-closed default used when no RTC coordinator is wired."""

    def terminate_and_wait(self, device_id: str, session_ids: list[str]) -> list[str]:
        raise RuntimeError("RTC termination capability unavailable")
