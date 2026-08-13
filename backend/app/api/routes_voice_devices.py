"""设备生命周期路由（SPEC §5 设备端点 / AC-02 / AC-15）

owner Bearer + nonce 保护 pairing-code/revoke；register 以 pairing_code 为 bootstrap 主体。
只输出锁定错误码；不记录 credential/nonce/secret。
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..voice.auth import AuthError, CredentialPrincipal
from ..voice.config import VoiceSecurityConfig, runtime_missing
from ..voice.devices import (
    DeviceNotFoundError,
    DeviceService,
    PairingInvalidError,
    RevokeTerminationError,
)
from ..voice.errors import HTTP_STATUS, error_payload

logger = logging.getLogger(__name__)


class CreatePairingCodeRequest(BaseModel):
    device_name_hint: str | None = Field(None, min_length=1, max_length=80)
    platform: str = Field(..., pattern="^(android)$")


class RegisterDeviceRequest(BaseModel):
    pairing_code: str = Field(..., min_length=20, max_length=256)
    device_name: str = Field(..., min_length=1, max_length=80)
    platform: str = Field(..., pattern="^(android)$")


class RevokeDeviceRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=200)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def build_device_router(*, store, validator, nonces, limiter,
                        security: VoiceSecurityConfig, devices: DeviceService) -> APIRouter:
    router = APIRouter(tags=["devices"])

    def missing() -> list[str]:
        return runtime_missing(security)

    def error(code: int, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(status_code=HTTP_STATUS[code], content=error_payload(code),
                            headers=headers)

    def owner_or_error(request: Request):
        if missing():
            return error(50300)
        token = None
        auth = request.headers.get("authorization", "")
        if auth:
            scheme, _, token = auth.partition(" ")
            if scheme.lower() != "bearer":
                token = None
            else:
                token = token.strip() or None
        if token is None:
            return error(40101)
        try:
            return validator.verify_owner(token)
        except AuthError as e:
            return error(e.code)

    def consume_nonce_or_error(principal: CredentialPrincipal, nonce: str):
        if not nonces.consume(principal, nonce):
            return error(40102)
        return None

    def limit_or_error(subject: str, request: Request, route: str):
        allowed, retry_after = limiter.check(subject, _client_ip(request), route)
        if not allowed:
            return error(42901, headers={"Retry-After": str(retry_after)})
        return None

    @router.post("/api/v1/voice/devices/pairing-code")
    async def create_pairing_code(req: CreatePairingCodeRequest, request: Request):
        principal = owner_or_error(request)
        if isinstance(principal, JSONResponse):
            return principal
        nonce = request.headers.get("x-request-nonce", "")
        denied = consume_nonce_or_error(principal, nonce)
        if denied is not None:
            return denied
        denied = limit_or_error(principal.subject_id, request, "voice:pairing")
        if denied is not None:
            return denied
        try:
            _code, meta = devices.create_pairing_code(
                principal.subject_id, req.platform, req.device_name_hint
            )
        except ValueError:
            return error(40001)
        return {"code": 0, "data": meta, "message": ""}

    @router.post("/api/v1/voice/devices/register", status_code=201)
    async def register_device(req: RegisterDeviceRequest, request: Request):
        if missing():
            return error(50300)
        nonce = request.headers.get("x-request-nonce", "")
        # bootstrap 主体：pairing_code 哈希（防重放绑定配对码本身）
        subject = "pairing:" + hashlib.sha256(req.pairing_code.encode("utf-8")).hexdigest()[:32]
        principal = CredentialPrincipal("pairing", subject, subject)
        denied = consume_nonce_or_error(principal, nonce)
        if denied is not None:
            return denied
        denied = limit_or_error("register", request, "voice:register")
        if denied is not None:
            return denied
        try:
            reg = devices.register_device(req.pairing_code, req.device_name, req.platform)
        except PairingInvalidError:
            return error(40901)
        data = {
            "device_id": reg.device_id,
            "credential_id": reg.credential_id,
            "credential_secret": reg.credential_secret,
            "expires_at": reg.expires_at,
        }
        return {"code": 0, "data": data, "message": ""}

    @router.get("/api/v1/voice/devices")
    async def list_devices(request: Request, page: int = 1, limit: int = 20):
        principal = owner_or_error(request)
        if isinstance(principal, JSONResponse):
            return principal
        denied = limit_or_error(principal.subject_id, request, "voice:devices")
        if denied is not None:
            return denied
        page = max(1, page)
        limit = min(100, max(1, limit))
        items = devices.list_devices()
        total = len(items)
        start = (page - 1) * limit
        data = {
            "items": items[start:start + limit],
            "total": total,
            "page": page,
            "limit": limit,
            "hasMore": start + limit < total,
        }
        return {"code": 0, "data": data, "message": ""}

    @router.post("/api/v1/voice/devices/{device_id}/revoke")
    async def revoke_device(device_id: str, req: RevokeDeviceRequest, request: Request):
        principal = owner_or_error(request)
        if isinstance(principal, JSONResponse):
            return principal
        nonce = request.headers.get("x-request-nonce", "")
        denied = consume_nonce_or_error(principal, nonce)
        if denied is not None:
            return denied
        denied = limit_or_error(principal.subject_id, request, "voice:revoke")
        if denied is not None:
            return denied
        try:
            result = devices.revoke_device(device_id, req.reason)
        except DeviceNotFoundError:
            return error(40401)
        except RevokeTerminationError:
            logger.warning("device revoke termination failed device=%s", device_id)
            # 2026-08-13：50301 termination_unconfirmed（可重试），不再混用 504 网关超时语义
            return error(50301)
        data = {
            "device_id": result["device_id"],
            "status": "revoked",
            "revoked_at": result["revoked_at"],
            "terminated_session_ids": result["terminated_session_ids"],
        }
        return {"code": 0, "data": data, "message": ""}

    return router
