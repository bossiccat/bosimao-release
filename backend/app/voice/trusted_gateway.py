"""Trusted reverse-proxy boundary for internal rtc_bridge identity."""
from __future__ import annotations

import hmac
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import CredentialValidator

_TRUSTED_SCOPE_KEY = "voice_trusted_gateway"
_STRIPPED_HEADERS = {
    b"x-client-certificate-thumbprint",
    b"x-client-certificate-verified",
}


class TrustedGatewayIdentityMiddleware:
    """Convert a loopback gateway assertion into server-owned request identity.

    Client certificate headers are always removed. Only a request arriving from an
    explicitly trusted source and presenting the reverse-proxy shared assertion can
    receive the server-side scope marker consumed by the hello redemption route.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        gateway_assertion_hash: str,
        certificate_binding: str,
        allowed_hosts: Iterable[str] = ("127.0.0.1", "::1"),
    ) -> None:
        self.app = app
        self.gateway_assertion_hash = gateway_assertion_hash
        self.certificate_binding = certificate_binding
        self.allowed_hosts = frozenset(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers", []))
        assertion = next(
            (value.decode("utf-8") for key, value in headers
             if key.lower() == b"x-internal-gateway-assertion"),
            "",
        )
        scope["headers"] = [
            (key, value) for key, value in headers
            if key.lower() not in _STRIPPED_HEADERS
        ]
        client = scope.get("client")
        source_host = client[0] if client else ""
        assertion_hash = CredentialValidator.hash_credential(assertion)
        trusted = (
            source_host in self.allowed_hosts
            and bool(self.gateway_assertion_hash)
            and bool(self.certificate_binding)
            and hmac.compare_digest(assertion_hash, self.gateway_assertion_hash)
        )
        if trusted:
            scope.setdefault("state", {})[_TRUSTED_SCOPE_KEY] = {
                "certificate_binding": self.certificate_binding,
            }
        await self.app(scope, receive, send)


def trusted_certificate_binding(scope: Scope) -> str | None:
    identity = scope.get("state", {}).get(_TRUSTED_SCOPE_KEY)
    if not isinstance(identity, dict):
        return None
    binding = identity.get("certificate_binding")
    return binding if isinstance(binding, str) and binding else None
