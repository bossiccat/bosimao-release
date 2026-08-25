"""HTTP mapping for commercial voice termination operations."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..voice.control_plane import IdempotencyConflict, InvalidTerminationState, SessionLedger
from .routes_voice_wake import build_wake_router
from .voice_termination_contract import (
    AckReportRequest, RetryTerminationRequest, TerminateSessionRequest,
    error_response, status_url, termination_status_data,
)

logger = logging.getLogger(__name__)


def build_termination_router(*, ledger: SessionLedger, guard=None,
                             reporter_resolver=None, rtc_service=None) -> APIRouter:
    router = APIRouter(tags=["Termination"])

    @router.post("/api/v1/voice/sessions/{session_id}/terminate", status_code=202)
    async def terminate_session(session_id: str, req: TerminateSessionRequest,
                                request: Request):
        if guard is not None:
            denied = guard(request, "terminate")
            if denied is not None:
                return denied
        if req.session_id != session_id:
            return error_response(40916)
        try:
            record = ledger.begin_termination(
                session_id=req.session_id, generation=req.generation,
                request_id=req.request_id, payload=req.model_dump(),
            )
        except IdempotencyConflict:
            return error_response(40912)
        except InvalidTerminationState as exc:
            return error_response(exc.code)
        except Exception:  # noqa: BLE001
            logger.exception("terminate failed session=%s", session_id)
            return error_response(50301)
        return {"code": 0, "data": {
            "termination_id": record["termination_id"],
            "session_id": record["session_id"], "generation": record["generation"],
            "state": "TERMINATING",
            "status_url": status_url(record["session_id"], record["termination_id"]),
        }, "message": ""}

    @router.get("/api/v1/voice/sessions/{session_id}/termination/{termination_id}")
    async def get_termination(session_id: str, termination_id: str, request: Request):
        if guard is not None:
            denied = guard(request, "termination_status")
            if denied is not None:
                return denied
        try:
            record = ledger.get_termination(termination_id)
        except InvalidTerminationState as exc:
            return error_response(exc.code)
        except Exception:  # noqa: BLE001
            logger.exception("get termination failed tid=%s", termination_id)
            return error_response(50301)
        if record["session_id"] != session_id:
            return error_response(40403)
        return {"code": 0, "data": termination_status_data(record), "message": ""}

    @router.post(
        "/api/v1/voice/sessions/{session_id}/termination/{termination_id}/retry",
        status_code=202,
    )
    async def retry_termination(session_id: str, termination_id: str,
                                req: RetryTerminationRequest, request: Request):
        if guard is not None:
            denied = guard(request, "retry")
            if denied is not None:
                return denied
        try:
            record = ledger.retry_termination(
                session_id=session_id, parent_termination_id=termination_id,
                request_id=req.request_id, reason=req.reason,
            )
        except IdempotencyConflict:
            return error_response(40912)
        except InvalidTerminationState as exc:
            return error_response(exc.code)
        except Exception:  # noqa: BLE001
            logger.exception("retry failed parent=%s", termination_id)
            return error_response(50301)
        return {"code": 0, "data": {
            "termination_id": record["termination_id"],
            "parent_termination_id": record["parent_termination_id"],
            "session_id": record["session_id"], "generation": record["generation"],
            "state": "TERMINATING",
            "status_url": status_url(record["session_id"], record["termination_id"]),
        }, "message": ""}

    @router.post(
        "/api/v1/voice/sessions/{session_id}/termination/{termination_id}/acknowledgements",
        status_code=202,
    )
    async def report_acknowledgement(session_id: str, termination_id: str,
                                     req: AckReportRequest, request: Request):
        if guard is not None:
            denied = guard(request, "termination_ack")
            if denied is not None:
                return denied
        if reporter_resolver is None:
            return error_response(50301)
        reporter = reporter_resolver(request)
        if reporter is None:
            return error_response(40901)
        if req.session_id != session_id:
            return error_response(40403)
        try:
            record = ledger.record_ack(
                termination_id=termination_id, session_id=req.session_id,
                device_id=req.device_id, room_id=req.room_id,
                generation=req.generation, acknowledgement=req.acknowledgement,
                reporter=reporter, result=req.result, error_code=req.error_code,
            )
        except InvalidTerminationState as exc:
            return error_response(
                exc.code if exc.code in {40403, 40901, 40916, 40917} else 40901
            )
        except Exception:  # noqa: BLE001
            logger.exception("ack report failed tid=%s", termination_id)
            return error_response(50301)
        return {"code": 0, "data": {
            "termination_id": record["termination_id"],
            "session_id": record["session_id"], "generation": record["generation"],
            "acknowledgement": req.acknowledgement,
            "ack_result": record["acknowledgements"][req.acknowledgement],
            "result": record["result"],
            "status_url": status_url(record["session_id"], record["termination_id"]),
        }, "message": ""}

    router.include_router(build_wake_router(
        ledger=ledger, guard=guard, rtc_service=rtc_service,
    ))
    return router
