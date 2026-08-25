"""Atomic orchestration for sidecar RTC credentials and hello proof issuance."""
from __future__ import annotations

from dataclasses import dataclass

from .hello_service import HelloProofService
from .repositories.pending_sessions import PendingSessionRepository
from .rtc_session import RtcSessionService


class SignClaimRejected(Exception):
    """The claim is absent, expired, consumed, revoked, or not in SIGNING state."""


@dataclass(frozen=True)
class SidecarSignRequest:
    session_id: str
    device_id: str
    claim_token: str
    user_id: str


class SidecarSignService:
    def __init__(
        self,
        pending: PendingSessionRepository,
        rtc: RtcSessionService,
        hello: HelloProofService,
    ) -> None:
        self._pending = pending
        self._rtc = rtc
        self._hello = hello

    def sign(self, request: SidecarSignRequest) -> dict:
        context = self._pending.get_signing_context(
            request.session_id, request.device_id, request.claim_token
        )
        if context is None:
            raise SignClaimRejected
        data = self._rtc.sign_bound(
            session_id=context["session_id"],
            device_id=context["device_id"],
            room_id=context["room_id"],
            user_id=request.user_id,
            generation=context["generation"],
        )
        hello, hello_expires_at = self._hello.issue(
            {
                "session_id": context["session_id"],
                "device_id": context["device_id"],
                "room_id": context["room_id"],
                "sidecar_user_id": request.user_id,
                "generation": context["generation"],
            },
            claim_token_hash=context["claim_token_hash"],
        )
        data["hello"] = hello
        data["hello_expires_at"] = hello_expires_at
        return data
