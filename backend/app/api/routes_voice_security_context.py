"""Shared security context and guards for commercial voice routes."""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from ..voice.auth import AuthError, CredentialPrincipal
from ..voice.config import runtime_missing
from ..voice.errors import HTTP_STATUS, error_payload
from ..voice.privacy import PrivacyService
from ..voice.sidecar_sign_service import SidecarSignService
from ..voice.timefmt import as_epoch

logger = logging.getLogger(__name__)


class SecuredVoiceDeps:
    def __init__(self, store, service, validator, nonces, limiter, security, devices,
                 privacy: PrivacyService, hello_service, user_sig_cipher=None,
                 ingest_ticket_signer=None) -> None:
        self.store = store
        self.service = service
        self.validator = validator
        self.nonces = nonces
        self.limiter = limiter
        self.security = security
        self.devices = devices
        self.privacy = privacy
        # 外带上行 ingest ticket 签发器（用 hello 同一把私钥，aud 区分）。
        # 未装配 → 端点 503，绝不静默降级。
        self.ingest_ticket_signer = ingest_ticket_signer
        self.sidecar_sign = (
            SidecarSignService(store.pending_sessions, service, hello_service)
            if hello_service is not None else None
        )
        from ..voice.control_plane import SessionLedger
        # user_sig_cipher 由上层注入（密钥来自 env/Secret）；None 时 wake 签发 fail-closed。
        self.ledger = SessionLedger(store, user_sig_cipher=user_sig_cipher)

    def runtime_missing(self) -> list[str]:
        return runtime_missing(self.security)

    def cloud_processing_gate(self) -> JSONResponse | None:
        try:
            if self.privacy.get("cloud_processing_enabled"):
                return None
        except Exception:  # noqa: BLE001
            logger.exception("privacy cloud_processing read failed")
        return self.error(40301)

    @staticmethod
    def resolve_bearer(authorization: str) -> str | None:
        scheme, _, token = authorization.partition(" ")
        return token.strip() if scheme.lower() == "bearer" and token else None

    @staticmethod
    def error(code: int, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_STATUS[code], content=error_payload(code), headers=headers
        )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def resolve_status_principal(deps: SecuredVoiceDeps, token: str) -> CredentialPrincipal | None:
    for verify in (deps.validator.verify_device, deps.validator.verify_sidecar):
        try:
            return verify(token)
        except AuthError:
            continue
    return None


_REPORTER_BY_TYPE = {
    "device": "android", "sidecar": "sidecar",
    "rtc_bridge": "rtc_bridge", "brain": "brain",
}


def termination_guard(deps: SecuredVoiceDeps):
    def guard(request: Request, action: str):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        if action == "termination_status":
            principal = resolve_status_principal(deps, token)
            if principal is None:
                return deps.error(40101)
        elif action == "termination_ack":
            principal = None
            reporter = None
            for verify in (deps.validator.verify_device, deps.validator.verify_sidecar,
                           deps.validator.verify_rtc_bridge, deps.validator.verify_brain):
                try:
                    principal = verify(token)
                    reporter = _REPORTER_BY_TYPE.get(principal.type)
                    break
                except AuthError:
                    continue
            if principal is None or reporter is None:
                return deps.error(40101)
            if not deps.nonces.consume(principal, request.headers.get("x-request-nonce", "")):
                return deps.error(40102)
            request.state.ack_reporter = reporter
        else:
            try:
                principal = deps.validator.verify_device(token)
            except AuthError as exc:
                return deps.error(exc.code)
            if not deps.nonces.consume(principal, request.headers.get("x-request-nonce", "")):
                return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, client_ip(request), f"voice:{action}"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        return None
    return guard


def ack_reporter_resolver(request: Request) -> str | None:
    return getattr(request.state, "ack_reporter", None)


def record_issued(deps: SecuredVoiceDeps, data: dict, device_id: str,
                  event_type: str) -> None:
    if deps.devices is not None:
        try:
            deps.devices.record_session_issued(
                data["session_id"], device_id, data["user_sig"],
                as_epoch(data["expires_at"]),
            )
            return
        except Exception:  # noqa: BLE001
            logger.warning("session fingerprint record failed device=%s", device_id)
    record_session_event(deps, data["session_id"], device_id, event_type, "IN_ROOM")


def record_session_event(deps: SecuredVoiceDeps, session_id: str, device_id: str,
                         event_type: str, state: str) -> None:
    try:
        deps.store.write_session_event(
            session_id=session_id, device_id=device_id, event_type=event_type, state=state
        )
    except Exception:  # noqa: BLE001
        logger.warning("session event write failed device=%s", device_id)
