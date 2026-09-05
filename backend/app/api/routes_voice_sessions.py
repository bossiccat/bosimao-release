"""Issue, claim and sign commercial voice sessions."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..voice.auth import AuthError
from ..voice.repositories.hello_proofs import HelloProofConflict
from ..voice.sidecar_sign_service import (
    SidecarSignRequest, SignClaimRejected,
)
from ..voice.timefmt import as_epoch, epoch_to_iso8601
from .routes_voice_security_context import (
    SecuredVoiceDeps, client_ip, record_issued,
)

logger = logging.getLogger(__name__)


class CreateDeviceSessionRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    entry_point: str = Field(..., pattern="^(main|overlay|notification)$")


class CreateSidecarSessionRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    claim_token: str = Field(..., min_length=32, max_length=256)
    device_id: str = Field(..., min_length=1, max_length=64)
    user_id: str = Field(..., pattern="^jax-pc-sidecar$")


def build_session_router(deps: SecuredVoiceDeps) -> APIRouter:
    router = APIRouter(tags=["voice"])

    @router.post("/api/v1/voice/session", status_code=201)
    async def voice_session(req: CreateDeviceSessionRequest, request: Request):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        try:
            principal = deps.validator.verify_device(token)
        except AuthError as exc:
            return deps.error(exc.code)
        if principal.subject_id != req.device_id:
            return deps.error(40001)
        if not deps.nonces.consume(principal, request.headers.get("x-request-nonce", "")):
            return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, client_ip(request), "voice:session"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        denied = deps.cloud_processing_gate()
        if denied is not None:
            return denied
        try:
            # 2026-09-05：issue + 入账同步状态机挪线程池（与 sign/redeem 同款，
            # 防占住事件循环拖慢同 loop 全部端点）
            data = await run_in_threadpool(deps.service.issue, req.device_id)
            data["generation"] = 0
            await run_in_threadpool(
                deps.ledger.create_session,
                session_id=data["session_id"], device_id=principal.subject_id,
                room_id=data["room_id"], generation=0, state="SIGNING",
            )
            await run_in_threadpool(
                deps.store.enqueue_pending_session,
                data["session_id"], principal.subject_id, data["room_id"],
                0, as_epoch(data["expires_at"]),
            )
        except Exception:  # noqa: BLE001
            logger.exception("secured session issue failed device=%s", req.device_id)
            return deps.error(50300)
        record_issued(deps, data, principal.subject_id, "issued")
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
        except AuthError as exc:
            return deps.error(exc.code)
        if not deps.nonces.consume(principal, request.headers.get("x-request-nonce", "")):
            return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, client_ip(request), "voice:pending"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        claim = deps.store.claim_pending_session()
        if claim is not None and "expires_at" in claim:
            # OpenAPI PendingSessionIntent.expires_at 声明 date-time；epoch → ISO8601
            claim["expires_at"] = epoch_to_iso8601(claim["expires_at"])
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
        except AuthError as exc:
            return deps.error(exc.code)
        if not deps.nonces.consume(principal, request.headers.get("x-request-nonce", "")):
            return deps.error(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, client_ip(request), "voice:sign"
        )
        if not allowed:
            return deps.error(42901, headers={"Retry-After": str(retry_after)})
        denied = deps.cloud_processing_gate()
        if denied is not None:
            return denied
        if deps.sidecar_sign is None:
            return deps.error(50303)
        try:
            # 2026-09-05：sign 同步状态机（SQLite BEGIN IMMEDIATE + Ed25519）挪线程池——
            # 与 hello-redeem（dea512e）同款修复：同步状态机直跑 async 端点会占住
            # 事件循环。真机实证（19:18-19:20）：sign 往返挂 111s，hello proof
            # TTL=60s 在等待中过期 → 兑付 40112 → sidecar 兑付失败退出。
            data = await run_in_threadpool(
                deps.sidecar_sign.sign,
                SidecarSignRequest(
                    session_id=req.session_id, device_id=req.device_id,
                    claim_token=req.claim_token, user_id=req.user_id,
                ),
            )
        except SignClaimRejected:
            return deps.error(40901)
        except HelloProofConflict as exc:
            return deps.error(40901 if exc.code == 40914 else exc.code)
        except Exception:  # noqa: BLE001
            logger.exception("secured session sign unavailable device=%s", req.device_id)
            return deps.error(50303)
        record_issued(deps, data, req.device_id, "sidecar_sign")
        return {"code": 0, "data": data, "message": ""}

    return router
