"""Schemas and response helpers for voice termination routes."""
from __future__ import annotations

from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..voice.timefmt import epoch_to_iso8601

CODE_TO_HTTP = {
    40402: 404, 40403: 404, 40901: 409, 40912: 409,
    40913: 409, 40916: 409, 40917: 409, 50301: 503,
    50302: 503,
}
CODE_MESSAGES = {
    40402: "session_not_found", 40403: "termination_not_found",
    40901: "state_conflict", 40912: "idempotency_key_payload_mismatch",
    40913: "termination_not_retryable", 40916: "request_id_reused",
    40917: "stale_or_conflicting_generation", 50301: "termination_unconfirmed",
}


def error_response(code: int) -> JSONResponse:
    return JSONResponse(
        status_code=CODE_TO_HTTP.get(code, 409),
        content={"code": code, "data": None, "message": CODE_MESSAGES.get(code, "error")},
    )


def status_url(session_id: str, termination_id: str) -> str:
    return f"/api/v1/voice/sessions/{session_id}/termination/{termination_id}"


def termination_status_data(record: dict[str, Any]) -> dict[str, Any]:
    result = record["result"]
    scope = "root" if record["operation"] == "terminate" else "retry_child"
    # terminal_at 在 DB 中是 epoch REAL；OpenAPI 声明 date-time，HTTP 边界序列化为 ISO8601
    terminal_at = record["terminal_at"]
    data = {
        "type": "session.terminating" if result == "pending" else "session.terminated",
        "scope": scope,
        "status_variant": f"{scope}_{result}",
        "termination_id": record["termination_id"],
        "session_id": record["session_id"],
        "generation": record["generation"],
        "result": result,
        "acknowledgements": record["acknowledgements"],
        "terminal_at": (
            epoch_to_iso8601(terminal_at)
            if terminal_at is not None else None
        ),
        "retryable": result in ("partial", "timeout"),
    }
    if scope == "retry_child":
        data["parent_termination_id"] = record["parent_termination_id"]
    return data


class TerminateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1, max_length=64)
    generation: int = Field(..., ge=0)
    request_id: str = Field(..., min_length=1)
    reason: Literal[
        "user_stop", "remote_leave", "app_shutdown", "security_revoke", "error_recovery"
    ]
    requested_at: str = Field(..., min_length=1)


class RetryTerminationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(..., min_length=1)
    reason: Literal["retry_failed_acknowledgements", "retry_timeout"]


class AckReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledgement: Literal[
        "android_trtc_left", "sidecar_trtc_left", "bridge_drained_closed",
        "apm_cancelled_closed", "brain_turns_sealed",
    ]
    result: Literal["confirmed", "failed"]
    session_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1, max_length=64)
    generation: int = Field(..., ge=0)
    reported_at: str = Field(..., min_length=1)
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _error_code_matches_result(self) -> "AckReportRequest":
        if self.result == "failed" and not self.error_code:
            raise ValueError("error_code is required when result is failed")
        if self.result == "confirmed" and self.error_code:
            raise ValueError("error_code must be omitted when result is confirmed")
        return self


class PreviousResourceRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1, max_length=64)
    user_sig_fingerprint: str = Field(..., min_length=16, max_length=128)


class WakeSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(..., min_length=1)
    prior_session_id: str = Field(..., min_length=1)
    prior_generation: int = Field(..., ge=0)
    previous_resource_refs: PreviousResourceRefs
    wake_event_id: str = Field(..., min_length=1)
    detected_at: str = Field(..., min_length=1)
    kws_instance_id: str = Field(..., min_length=1, max_length=128)

    @model_validator(mode="after")
    def _prior_refs_consistent(self) -> "WakeSessionRequest":
        if self.prior_session_id != self.previous_resource_refs.session_id:
            raise ValueError("prior_session_id must equal previous_resource_refs.session_id")
        return self
