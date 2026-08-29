"""KWS wake operation router."""
from __future__ import annotations

import logging
import uuid
from functools import partial

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..voice.control_plane import InvalidTerminationState
from .guarded_route import GuardedAPIRoute, guarded
from .voice_termination_contract import (
    CODE_MESSAGES, CODE_TO_HTTP, WakeSessionRequest, error_response,
)

logger = logging.getLogger(__name__)


def build_wake_router(*, ledger, guard=None, rtc_service=None) -> APIRouter:
    # 守卫经 route_class 前置：未认证请求 401 先于 body 校验（见 guarded_route 模块说明）。
    router = APIRouter(
        tags=["Termination"],
        route_class=partial(GuardedAPIRoute, guard=guard),
    )

    @router.post("/api/v1/voice/sessions/wake", status_code=201)
    @guarded("wake")
    async def wake_session(req: WakeSessionRequest):
        if rtc_service is None:
            logger.error("wake endpoint mounted without rtc_service")
            return error_response(50301)
        session_id = str(uuid.uuid4())
        try:
            issued = rtc_service.sign(req.device_id, req.device_id)
        except Exception:  # noqa: BLE001
            logger.exception("wake rtc issuance failed device=%s", req.device_id)
            return error_response(50301)
        try:
            record = ledger.consume_wake(
                session_id=session_id, device_id=req.device_id,
                prior_session_id=req.prior_session_id,
                prior_generation=req.prior_generation,
                wake_event_id=req.wake_event_id,
                user_sig=issued["user_sig"], expires_at=issued["expires_at"],
                detected_at=req.detected_at, kws_instance_id=req.kws_instance_id,
                user_id=issued["user_id"],
            )
        except InvalidTerminationState as exc:
            if exc.code == 40402:
                return error_response(40402)
            return JSONResponse(
                status_code=CODE_TO_HTTP.get(exc.code, 409),
                content={
                    "code": exc.code, "data": None,
                    "message": (
                        "wake_event_replayed_or_expired" if exc.code == 40913
                        else CODE_MESSAGES.get(exc.code, "state_conflict")
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("wake failed device=%s event=%s", req.device_id,
                             req.wake_event_id)
            return error_response(50301)
        return {
            "code": 0,
            "data": {
                "wake_event_id": record["wake_event_id"],
                "prior_session_id": record["prior_session_id"],
                "prior_generation": record["prior_generation"],
                "previous_resource_refs": req.previous_resource_refs.model_dump(),
                "generation_transition": {
                    "from_generation": record["prior_generation"],
                    "to_generation": record["generation"], "increment": 1,
                },
                "resource_freshness": {
                    "session_id_changed": True, "room_id_changed": True,
                    "user_sig_changed": True,
                },
                "generation": record["generation"],
                "session_id": record["session_id"], "room_id": record["room_id"],
                "user_id": record["user_id"], "user_sig": record["user_sig"],
                "expires_at": record["expires_at"], "scene": "trtc_full_duplex",
            },
            "message": "",
        }

    return router
