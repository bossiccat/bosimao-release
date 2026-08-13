"""隐私开关路由（SPEC §9.2 / AC-17 / ADR-021 D2）

- GET   /api/v1/privacy —— 读全部开关（owner / device / sidecar 任一有效主体）
- PATCH /api/v1/privacy/{setting} —— owner only + X-Request-Nonce + 限流，设单开关

复用 routes_voice_secured 的主体解析模式：读用「任一有效主体」，写用 owner。
只输出锁定错误码；不记录 credential/nonce/secret。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..voice.auth import AuthError, CredentialPrincipal
from ..voice.config import VoiceSecurityConfig, runtime_missing
from ..voice.errors import HTTP_STATUS, error_payload
from ..voice.privacy import PrivacyService

# setting 路径取值 → *_enabled 键（ADR-021 D2 契约）
SETTING_PATH_KEYS = {
    "cloud_processing": "cloud_processing_enabled",
    "microphone": "microphone_enabled",
    "background_conversation": "background_conversation_enabled",
    "desktop_capture": "desktop_capture_enabled",
    "transcript_persistence": "transcript_persistence_enabled",
}
ALL_SETTING_KEYS = list(SETTING_PATH_KEYS.values())


class SetPrivacyRequest(BaseModel):
    enabled: bool


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _resolve_bearer(authorization: str) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def build_privacy_router(*, validator, nonces, limiter,
                         security: VoiceSecurityConfig, privacy: PrivacyService) -> APIRouter:
    router = APIRouter(tags=["privacy"])

    def missing() -> list[str]:
        return runtime_missing(security)

    def error(code: int, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(status_code=HTTP_STATUS[code], content=error_payload(code),
                            headers=headers)

    def resolve_principal(token: str) -> CredentialPrincipal | None:
        """owner / device / sidecar 任一有效主体（对齐 _resolve_status_principal 模式）"""
        for verify in (validator.verify_device, validator.verify_sidecar, validator.verify_owner):
            try:
                return verify(token)
            except AuthError:
                continue
        return None

    def owner_or_error(request: Request):
        """owner only（对齐 routes_voice_devices.owner_or_error 模式）"""
        if missing():
            return error(50300)
        token = _resolve_bearer(request.headers.get("authorization", ""))
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

    @router.get("/api/v1/privacy")
    async def get_privacy(request: Request):
        if missing():
            return error(50300)
        token = _resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return error(40101)
        principal = resolve_principal(token)
        if principal is None:
            return error(40101)
        denied = limit_or_error(principal.subject_id, request, "voice:privacy")
        if denied is not None:
            return denied
        settings = {key: privacy.get(key) for key in ALL_SETTING_KEYS}
        return {"code": 0, "data": {"settings": settings}, "message": ""}

    @router.patch("/api/v1/privacy/{setting}")
    async def patch_privacy(setting: str, req: SetPrivacyRequest, request: Request):
        principal = owner_or_error(request)
        if isinstance(principal, JSONResponse):
            return principal
        key = SETTING_PATH_KEYS.get(setting)
        if key is None:
            return error(40001)
        denied = consume_nonce_or_error(principal, request.headers.get("x-request-nonce", ""))
        if denied is not None:
            return denied
        denied = limit_or_error(principal.subject_id, request, "voice:privacy:set")
        if denied is not None:
            return denied
        try:
            result = privacy.set(key, req.enabled, actor=principal.subject_id)
        except ValueError:
            return error(40001)
        if result["action_result"] == "failed":
            return error(50302)
        return {
            "code": 0,
            "data": {
                "setting": key,
                "applied_at": result["applied_at"],
                "effective_value": result["effective_value"],
                "action_result": result["action_result"],
            },
            "message": "",
        }

    return router
