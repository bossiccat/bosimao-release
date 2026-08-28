"""Owner-only approval control plane for persistent agent threads."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..brain.agent_thread_registry import AgentThreadRegistry
from ..voice.auth import AuthError, CredentialValidator
from ..voice.errors import HTTP_STATUS, error_payload
from ..voice.nonce import NonceService
from ..voice.rate_limit import RateLimiter


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=32, max_length=256)


def create_agent_thread_router(*, registry: AgentThreadRegistry, validator: CredentialValidator,
                               nonces: NonceService, limiter: RateLimiter) -> APIRouter:
    router = APIRouter(prefix="/api/v1/brain/threads", tags=["brain"])

    def error(code: int, *, retry_after: int | None = None) -> JSONResponse:
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return JSONResponse(
            status_code=HTTP_STATUS[code], content=error_payload(code), headers=headers
        )

    @router.post("/{thread_id}/approve", response_model=None)
    async def approve_thread(thread_id: str, req: ApprovalRequest, request: Request) -> dict | JSONResponse:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return error(40101)
        try:
            principal = validator.verify_owner(token.strip())
        except AuthError as exc:
            return error(exc.code)
        nonce = request.headers.get("x-request-nonce", "")
        if not nonces.consume(principal, nonce):
            return error(40102)
        allowed, retry_after = limiter.check(
            principal.subject_id,
            request.client.host if request.client else "unknown",
            "brain:approve",
        )
        if not allowed:
            return error(42901, retry_after=retry_after)
        result = registry.approve(thread_id, req.approval_id)
        if result.get("error"):
            return error(40401 if result["error"] == "thread_not_found" else 40901)
        return {"code": 0, "data": result, "message": "审批已通过"}

    return router
