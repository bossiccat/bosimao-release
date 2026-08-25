"""Authentication, nonce, and rate-limit guard for termination routes."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from ..voice.auth import AuthError
from .routes_voice_stream import resolve_status_principal

_REPORTER_BY_PRINCIPAL_TYPE = {
    "device": "android",
    "sidecar": "sidecar",
    "rtc_bridge": "rtc_bridge",
    "brain": "brain",
}


def build_termination_guard(deps: Any):
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
            principal, reporter = _resolve_ack_principal(deps, token)
            if principal is None:
                return deps.error(40101)
            if not deps.nonces.consume(
                principal, request.headers.get("x-request-nonce", "")
            ):
                return deps.error(40102)
            request.state.ack_reporter = reporter
        else:
            try:
                principal = deps.validator.verify_device(token)
            except AuthError as exc:
                return deps.error(exc.code)
            if not deps.nonces.consume(
                principal, request.headers.get("x-request-nonce", "")
            ):
                return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, _client_ip(request), f"voice:{action}"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        return None

    return guard


def ack_reporter_resolver(request: Request) -> str | None:
    return getattr(request.state, "ack_reporter", None)


def _resolve_ack_principal(deps: Any, token: str):
    for verify in (
        deps.validator.verify_device,
        deps.validator.verify_sidecar,
        deps.validator.verify_rtc_bridge,
        deps.validator.verify_brain,
    ):
        try:
            principal = verify(token)
        except AuthError:
            continue
        reporter = _REPORTER_BY_PRINCIPAL_TYPE.get(principal.type)
        if reporter is not None:
            return principal, reporter
        return None, None
    return None, None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
