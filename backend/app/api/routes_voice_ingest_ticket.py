"""外带上行 ingest ticket 的 HTTP 映射（阶段 1：**仅签发**）。

设计：outputs/design-uplink-out-of-band-2026-09-18.md §1（鉴权模型）/§2（协议）。
本阶段只在控制面发票；手机与桥侧的接入是后续阶段，因此本端点当前是**惰性**的
（未装配签名器即 503，不签发任何东西）。

守卫用 `GuardedAPIRoute` 前置（见 guarded_route.py）：**未认证请求必须 401 而不是 422**，
否则会泄露 request body 的 schema 结构。这与 wake/termination 路由同一套机制。
"""
from __future__ import annotations

import logging
from functools import partial

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..voice.auth import AuthError
from ..voice.control_plane import InvalidTerminationState
from ..voice.ingest_ticket import (
    INGEST_ALLOWED_SESSION_STATES,
    INGEST_AUDIENCE,
)
from .guarded_route import GuardedAPIRoute, guarded
from .routes_voice_security_context import SecuredVoiceDeps, client_ip

logger = logging.getLogger(__name__)

_CODE_TO_HTTP = {
    40001: 400, 40101: 401, 40102: 401, 40402: 404, 40901: 409,
    42901: 429, 50300: 503, 50301: 503,
}
_CODE_MESSAGES = {
    40001: "invalid_device_or_user",
    40101: "auth_failed",
    40102: "nonce_replay",
    40402: "session_not_found",
    40901: "state_conflict",
    42901: "rate_limited",
    50300: "credential_unavailable",
    50301: "ingest_ticket_unavailable",
}

#: 守卫把已认证主体放在这里，端点只读它，不重复验签。
_SUBJECT_ATTR = "ingest_subject_id"


def error_response(code: int, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=_CODE_TO_HTTP.get(code, 409),
        content={"code": code, "data": None, "message": _CODE_MESSAGES.get(code, "error")},
        headers=headers,
    )


class IngestTicketRequest(BaseModel):
    """绑定既有会话的取票请求。"""

    device_id: str = Field(..., min_length=1, max_length=64)
    session_id: str = Field(..., min_length=1, max_length=64)


def _ingest_guard(deps: SecuredVoiceDeps):
    """设备 Bearer + 新鲜 nonce + 限流；通过后把主体写入 request.state。

    重放保护归位在这里（控制面**有** store）——媒体流本身不携带 nonce，
    这正是本设计能绕开「连续媒体无法逐帧 nonce」的原因。
    """

    def guard(request: Request, action: str):
        if deps.runtime_missing():
            return error_response(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return error_response(40101)
        try:
            principal = deps.validator.verify_device(token)
        except AuthError as exc:
            return error_response(exc.code)
        if not deps.nonces.consume(principal, request.headers.get("x-request-nonce", "")):
            return error_response(40102)
        allowed, retry_after = deps.limiter.check(
            principal.subject_id, client_ip(request), f"voice:{action}"
        )
        if not allowed:
            return error_response(42901, headers={"Retry-After": str(retry_after)})
        setattr(request.state, _SUBJECT_ATTR, principal.subject_id)
        return None

    return guard


def build_ingest_ticket_router(deps: SecuredVoiceDeps) -> APIRouter:
    router = APIRouter(
        tags=["voice"],
        route_class=partial(GuardedAPIRoute, guard=_ingest_guard(deps)),
    )

    @router.post("/api/v1/voice/session/ingest-ticket", status_code=201)
    @guarded("ingest_ticket")
    async def issue_ingest_ticket(req: IngestTicketRequest, request: Request):
        """为**既有**会话签发一张短时效 ingest ticket（外带上行用）。

        fail-closed 链：未装配签名器 → 503；会话不存在 → 404；非本设备会话 → 400；
        会话状态不可签发 → 409。任何一步不通过都**不签发**。
        """
        if deps.ingest_ticket_signer is None:
            # 未装配即拒，绝不静默降级成「无凭证可入站」。
            logger.warning("ingest ticket requested but signer is not wired")
            return error_response(50300)

        subject_id = getattr(request.state, _SUBJECT_ATTR, None)
        if not subject_id:
            # 守卫未跑（例如内部直连）——视为未认证。
            return error_response(40101)
        if req.device_id != subject_id:
            return error_response(40001)

        try:
            session = deps.ledger.get_session(req.session_id)
        except InvalidTerminationState as exc:
            return error_response(getattr(exc, "code", 40402))
        except Exception:  # noqa: BLE001
            logger.exception("ingest ticket session lookup failed sid=%s", req.session_id)
            return error_response(50301)

        # 跨设备越权：会话不属于该主体时一律拒（与 /session 的 40001 语义一致）。
        if session.get("device_id") != subject_id:
            return error_response(40001)

        if session.get("state") not in INGEST_ALLOWED_SESSION_STATES:
            return error_response(40901)

        try:
            issued = deps.ingest_ticket_signer.issue(
                session_id=session["session_id"],
                device_id=session["device_id"],
                room_id=session["room_id"],
                generation=session["generation"],
            )
        except Exception:  # noqa: BLE001 - 密钥不可用等一律 fail-closed
            logger.exception("ingest ticket issuance failed sid=%s", req.session_id)
            return error_response(50301)

        return {
            "code": 0,
            "data": {**issued, "audience": INGEST_AUDIENCE},
            "message": "",
        }

    return router
