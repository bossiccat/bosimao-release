"""Internal rtc_bridge hello proof redemption HTTP adapter."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..voice.auth import AuthError, CredentialValidator
from ..voice.hello_proof import HelloProofError
from ..voice.hello_service import HelloProofService
from ..voice.repositories.hello_proofs import HelloProofConflict
from ..voice.trusted_gateway import trusted_certificate_binding

logger = logging.getLogger(__name__)
_MESSAGES = {
    40021: "invalid_hello",
    40111: "hello_proof_invalid",
    40112: "hello_proof_expired",
    40113: "hello_replay",
    40114: "rtc_bridge_service_auth_failed",
    40914: "hello_state_conflict",
    50303: "hello_redemption_unavailable",
}
_STATUS = {40021: 400, 40111: 401, 40112: 401, 40113: 401, 40114: 401, 40914: 409, 50303: 503}


class AudioFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    encoding: Literal["pcm_s16le"]
    sample_rate_hz: Literal[16000]
    channels: Literal[1]
    frame_ms: Literal[20]
    frame_bytes: Literal[640]


class HelloRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hello"]
    proof: str = Field(..., min_length=1)
    nonce: str = Field(..., min_length=16, max_length=512)
    jti: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1, max_length=64)
    sidecar_user_id: str = Field(..., min_length=1, max_length=64)
    generation: int = Field(..., ge=0)
    protocol_version: Literal["1.0"]
    audio_format: AudioFormat


def _error(code: int) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS[code],
        content={"code": code, "data": None, "message": _MESSAGES[code]},
    )


def build_hello_router(
    *,
    validator: CredentialValidator,
    service: HelloProofService,
    expected_certificate_binding: str,
    gateway_assertion_hash: str = "",
) -> APIRouter:
    router = APIRouter(tags=["voice"])

    @router.post("/api/v1/voice/internal/rtc-bridge/hello-redeem")
    async def redeem_hello(request: Request, payload: object = Body(...)):
        authorization = request.headers.get("authorization", "")
        scheme, _, bearer = authorization.partition(" ")
        certificate = trusted_certificate_binding(request.scope)
        if (scheme.lower() != "bearer" or not bearer
                or certificate != expected_certificate_binding):
            return _error(40114)
        try:
            principal = validator.verify_rtc_bridge(bearer.strip())
            if principal.subject_id != "rtc_bridge":
                return _error(40114)
        except AuthError:
            return _error(40114)
        try:
            req = HelloRedeemRequest.model_validate(payload)
        except ValidationError:
            return _error(40021)
        try:
            data = service.redeem(req.model_dump())
        except (HelloProofError, HelloProofConflict) as exc:
            return _error(exc.code)
        except Exception:  # noqa: BLE001
            logger.exception("hello redemption unavailable")
            return _error(50303)
        return {"code": 0, "data": data, "message": ""}

    return router
