"""Composition facade for commercial secured voice routes."""
from __future__ import annotations

from fastapi import APIRouter

from ..voice.config import VoiceSecurityConfig, production_gate
from ..voice.nonce import NonceService
from ..voice.privacy import FakeRuntimeActions, PrivacyService
from ..voice.rate_limit import RateLimiter
from ..voice.rtc_session import RtcSessionService
from ..voice.storage import VoiceStore
from .routes_voice_security_context import (
    SecuredVoiceDeps, ack_reporter_resolver, termination_guard,
)
from .routes_voice_sessions import build_session_router
from .routes_voice_status_stream import build_status_stream_router


def create_secured_voice_router(
    *, store: VoiceStore, service: RtcSessionService, validator,
    nonces: NonceService, limiter: RateLimiter, security: VoiceSecurityConfig,
    devices=None, privacy: PrivacyService | None = None, hello_service=None,
    hello_certificate_binding: str = "", hello_gateway_assertion_hash: str = "",
    user_sig_cipher=None, ingest_ticket_signer=None,
) -> APIRouter:
    production_gate(security)
    if privacy is None:
        privacy = PrivacyService(store, FakeRuntimeActions())
    deps = SecuredVoiceDeps(
        store, service, validator, nonces, limiter, security,
        devices, privacy, hello_service,
        user_sig_cipher=user_sig_cipher,
        # 外带上行 ingest ticket 签发器（hello 同一把密钥，aud 区分）。
        # 不传 = 端点 503（惰性装配，见 routes_voice_ingest_ticket.py）。
        ingest_ticket_signer=ingest_ticket_signer,
    )
    router = APIRouter(tags=["voice"])

    if hello_service is not None:
        from .routes_voice_hello import build_hello_router
        router.include_router(build_hello_router(
            validator=validator,
            service=hello_service,
            expected_certificate_binding=hello_certificate_binding,
            # 调用方（PC 入口 / cloudapi）传入的 gateway assertion hash 必须继续下传，
            # 否则 trusted-gateway 校验参数在装配层被吞掉。
            gateway_assertion_hash=hello_gateway_assertion_hash,
        ))
    if devices is not None:
        from .routes_voice_devices import build_device_router
        router.include_router(build_device_router(
            store=store, validator=validator, nonces=nonces,
            limiter=limiter, security=security, devices=devices,
        ))

    from .routes_voice_privacy import build_privacy_router
    router.include_router(build_privacy_router(
        validator=validator, nonces=nonces, limiter=limiter,
        security=security, privacy=privacy,
    ))

    from .routes_voice_termination import build_termination_router
    router.include_router(build_termination_router(
        ledger=deps.ledger,
        guard=termination_guard(deps),
        reporter_resolver=ack_reporter_resolver,
        rtc_service=service,
    ))
    router.include_router(build_session_router(deps))
    router.include_router(build_status_stream_router(deps))

    # 外带上行取票端点：无条件挂载。未装配签名器时端点自身返回 503——
    # 这样「端点存在但不可用」是显式的，不会退化成 404（那会被误读成「没这个功能」）。
    from .routes_voice_ingest_ticket import build_ingest_ticket_router
    router.include_router(build_ingest_ticket_router(deps))
    return router
