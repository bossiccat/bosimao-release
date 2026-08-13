"""商业语音安全路由（SPEC §5 / AC-04；QA spec §5.4 真实组合）

统一保护：Bearer 主体校验（device/sidecar 隔离）→ nonce 原子消费 → device/IP 限流 →
生产 fail-closed。绝不输出 credential/nonce/userSig；错误只带锁定错误码。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..voice.auth import (
    AuthError,
    CredentialPrincipal,
    CredentialValidator,
)
from ..voice.config import VoiceSecurityConfig, production_gate, runtime_missing
from ..voice.errors import HTTP_STATUS, error_payload
from ..voice.nonce import NonceService
from ..voice.rate_limit import RateLimiter
from ..voice.rtc_session import RtcSessionService
from ..voice.storage import VoiceStore

logger = logging.getLogger(__name__)

WS_CLOSE_AUTH = 4401
WS_CLOSE_NONCE = 4402
WS_CLOSE_BINDING = 4403
WS_CLOSE_GATE = 4503
WS_CLOSE_HELLO_TIMEOUT = 4408


class CreateDeviceSessionRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    entry_point: str = Field(..., pattern="^(main|overlay|notification)$")


class CreateSidecarSessionRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    claim_token: str = Field(..., min_length=32, max_length=256)
    device_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(..., pattern="^jax-pc-sidecar$")


class _SecuredDeps:
    def __init__(self, store, service, validator, nonces, limiter, security, devices=None) -> None:
        self.store: VoiceStore = store
        self.service: RtcSessionService = service
        self.validator: CredentialValidator = validator
        self.nonces: NonceService = nonces
        self.limiter: RateLimiter = limiter
        self.security: VoiceSecurityConfig = security
        self.devices = devices

    def runtime_missing(self) -> list[str]:
        return runtime_missing(self.security)

    def resolve_bearer(self, authorization: str) -> str | None:
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        return token.strip()

    def error(self, code: int, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_STATUS[code],
            content=error_payload(code),
            headers=headers,
        )


def create_secured_voice_router(*, store: VoiceStore, service: RtcSessionService,
                                validator: CredentialValidator, nonces: NonceService,
                                limiter: RateLimiter, security: VoiceSecurityConfig,
                                devices=None) -> APIRouter:
    """装配商业安全路由；production=True 且缺必需能力时抛 ProductionGateError（拒绝启动）

    devices 为 None 时设备管理端点不注册（fail-closed：不提供匿名设备旁路）。
    """
    production_gate(security)
    deps = _SecuredDeps(store, service, validator, nonces, limiter, security, devices=devices)
    router = APIRouter(tags=["voice"])
    if devices is not None:
        from .routes_voice_devices import build_device_router

        router.include_router(
            build_device_router(store=store, validator=validator, nonces=nonces,
                                limiter=limiter, security=security, devices=devices)
        )

    @router.post("/api/v1/voice/session", status_code=201)
    async def voice_session(req: CreateDeviceSessionRequest, request: Request):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        try:
            principal = deps.validator.verify_device(token)
        except AuthError as e:
            return deps.error(e.code)
        if principal.subject_id != req.device_id:
            return deps.error(40001)
        nonce = request.headers.get("x-request-nonce", "")
        if not deps.nonces.consume(principal, nonce):
            return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, _client_ip(request), "voice:session"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        try:
            data = deps.service.issue(req.device_id)
        except Exception:  # noqa: BLE001
            logger.exception("secured session issue failed device=%s", req.device_id)
            return deps.error(50300)
        _record_issued(deps, data, principal.subject_id, "issued")
        try:
            deps.store.enqueue_pending_session(
                data["session_id"], principal.subject_id, data["room_id"], data["expires_at"]
            )
        except Exception:  # noqa: BLE001
            logger.exception("pending session enqueue failed device=%s", principal.subject_id)
            return deps.error(50300)
        return {"code": 0, "data": data, "message": ""}

    @router.get("/api/v1/voice/session/pending")
    async def voice_session_pending(request: Request):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        try:
            principal = deps.validator.verify_sidecar(token)
        except AuthError as e:
            return deps.error(e.code)
        nonce = request.headers.get("x-request-nonce", "")
        if not deps.nonces.consume(principal, nonce):
            return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, _client_ip(request), "voice:pending"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        claim = deps.store.claim_pending_session()
        return {"code": 0, "data": {"intents": [claim] if claim else []}, "message": ""}

    @router.post("/api/v1/voice/session/sign", status_code=201)
    async def voice_session_sign(req: CreateSidecarSessionRequest, request: Request):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        try:
            principal = deps.validator.verify_sidecar(token)
        except AuthError as e:
            return deps.error(e.code)
        nonce = request.headers.get("x-request-nonce", "")
        if not deps.nonces.consume(principal, nonce):
            return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, _client_ip(request), "voice:sign"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        claim = deps.store.consume_pending_sign_claim(
            req.session_id, req.device_id, req.claim_token
        )
        if claim is None:
            return deps.error(40901)
        try:
            data = deps.service.sign(req.device_id, req.user_id)
        except Exception:  # noqa: BLE001
            logger.exception("secured session sign failed device=%s", req.device_id)
            return deps.error(50300)
        if data["room_id"] != claim["room_id"]:
            logger.error("secured session claim room mismatch device=%s", req.device_id)
            return deps.error(40901)
        _record_issued(deps, data, req.device_id, "sidecar_sign")
        return {"code": 0, "data": data, "message": ""}

    @router.get("/api/v1/voice/status")
    async def voice_status(request: Request):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        principal = _resolve_status_principal(deps, token)
        if principal is None:
            return deps.error(40101)
        allowed, _retry = deps.limiter.check(principal.subject_id, _client_ip(request), "voice:status")
        if not allowed:
            return deps.error(42901)
        events = deps.store.list_session_events(principal.subject_id, limit=20)
        latest = events[0] if events else None
        data = {
            "session_id": latest["session_id"] if latest else None,
            "turn_id": None,
            "state": latest["state"] if latest and latest["state"] else "IDLE",
            "up_frame_count": 0,
            "up_bytes": 0,
            "down_frame_count": 0,
            "down_bytes": 0,
            "first_remote_audio_ts": None,
            "first_nonzero_playback_ts": None,
            "queue_depth": 0,
            "queue_high_watermark": 0,
            "queue_drops": 0,
            "backpressure_events": 0,
            "reconnects": 0,
            "error_code": None,
        }
        return {"code": 0, "data": data, "message": ""}

    @router.websocket("/api/v1/voice/stream")
    async def voice_stream(ws: WebSocket) -> None:
        if deps.runtime_missing():
            await ws.close(code=WS_CLOSE_GATE)
            return
        token = deps.resolve_bearer(ws.headers.get("authorization", ""))
        if token is None:
            await ws.close(code=WS_CLOSE_AUTH)
            return
        try:
            principal = deps.validator.verify_device(token)
        except AuthError:
            await ws.close(code=WS_CLOSE_AUTH)
            return
        nonce = ws.headers.get("x-request-nonce", "")
        if not deps.nonces.consume(principal, nonce):
            await ws.close(code=WS_CLOSE_NONCE)
            return
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except Exception:  # noqa: BLE001
            await ws.close(code=WS_CLOSE_HELLO_TIMEOUT)
            return
        if (not isinstance(hello, dict) or hello.get("type") != "hello"
                or hello.get("device_id") != principal.subject_id):
            await ws.close(code=WS_CLOSE_BINDING)
            return
        await ws.send_json({"type": "ready", "session_id": hello.get("session_id"),
                            "device_id": principal.subject_id})
        _record_session_event(deps, str(hello.get("session_id")), principal.subject_id,
                              "stream_ready", "IN_ROOM")
        try:
            while True:
                frame = await ws.receive_json()
                device = deps.store.get_device(principal.subject_id)
                if (device is None or device.status == "revoked"
                        or device.revoked_at is not None):
                    await ws.close(code=WS_CLOSE_AUTH, reason="credential_revoked")
                    break
                if isinstance(frame, dict) and frame.get("type") == "close":
                    break
        except WebSocketDisconnect:
            return

    return router


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _resolve_status_principal(deps: _SecuredDeps, token: str) -> CredentialPrincipal | None:
    """status 接受 device 或 sidecar 主体"""
    try:
        return deps.validator.verify_device(token)
    except AuthError:
        pass
    try:
        return deps.validator.verify_sidecar(token)
    except AuthError:
        return None


def _record_issued(deps: _SecuredDeps, data: dict, device_id: str, event_type: str) -> None:
    """签发成功后登记 userSig 指纹（devices 未装配时不登记，仅记录事件）"""
    if deps.devices is not None:
        try:
            deps.devices.record_session_issued(
                data["session_id"], device_id, data["user_sig"], data["expires_at"]
            )
            return
        except Exception:  # noqa: BLE001 - 指纹登记失败不阻断签发
            logger.warning("session fingerprint record failed device=%s", device_id)
    _record_session_event(deps, data["session_id"], device_id, event_type, "IN_ROOM")


def _record_session_event(deps: _SecuredDeps, session_id: str, device_id: str,
                          event_type: str, state: str) -> None:
    try:
        deps.store.write_session_event(
            session_id=session_id, device_id=device_id, event_type=event_type, state=state
        )
    except Exception:  # noqa: BLE001 - 可观测写入失败不阻断签发
        logger.warning("session event write failed device=%s", device_id)
