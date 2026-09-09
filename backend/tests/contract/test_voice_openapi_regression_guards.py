"""商业终止/唤醒端点 OpenAPI 契约负向对照与 40912 分支回归守卫。

与 test_voice_termination_openapi_validation.py 互补（该文件已接近 300 行门禁）：
本文件专注三类防回归守卫——
1. epoch float 回归：terminal_at / expires_at 若退化为数字，OAS30 date-time
   format checker 必须拒绝（证明正向测试的 format 校验是真实生效的）；
2. retry 40912 端到端：复用 root terminate 的 request_id 触发幂等负载不匹配，
   响应体必须命中 IdempotencyPayloadMismatchError 分支；
3. 契约结构：RetryTerminationStateConflict.oneOf 必须包含 40912 分支
   （2026-08-26 架构师 #28 裁决补齐项的回归保护）。

自包含 fixture/helper（tests/contract 无 __init__.py，rootdir 导入模式）。
"""
from __future__ import annotations

import hashlib
import uuid
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import RefResolver
from openapi_schema_validator import OAS30Validator, oas30_format_checker

from app.voice.control_plane import SessionLedger, VoiceStore
from app.voice.user_sig_cipher import UserSigCipher

ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = ROOT / "docs" / "api" / "commercial-voice-openapi.yaml"

warnings.filterwarnings(
    "ignore",
    message="jsonschema.RefResolver is deprecated*",
    category=DeprecationWarning,
)

ACK_NAMES = (
    "android_trtc_left",
    "sidecar_trtc_left",
    "bridge_drained_closed",
    "apm_cancelled_closed",
    "brain_turns_sealed",
)


def _load_spec() -> dict:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def spec() -> dict:
    return _load_spec()


@pytest.fixture(scope="module")
def make_validator(spec: dict):
    def _make(schema_ref: str) -> OAS30Validator:
        resolver = RefResolver.from_schema(spec)
        schema = {"$ref": f"#/components/schemas/{schema_ref}"}
        return OAS30Validator(schema, resolver=resolver,
                              format_checker=oas30_format_checker)
    return _make


def _make_client(tmp_path: Path) -> tuple[TestClient, SessionLedger]:
    from app.api.routes_voice_termination import build_termination_router
    from app.voice.rtc_session import RtcSessionConfig, RtcSessionService

    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    ledger = SessionLedger(
        store,
        user_sig_cipher=UserSigCipher(
            hashlib.sha256(b"task-b-user-sig-cipher-test-key").digest()
        ),
    )
    service = RtcSessionService(
        RtcSessionConfig(sdk_app_id=1600155678,
                         secret_key="fake-secret-key-for-test-only-0123456789",
                         room_prefix="jax-")
    )
    app = FastAPI()
    app.include_router(build_termination_router(ledger=ledger, rtc_service=service))
    return TestClient(app), ledger


def _create_session(ledger: SessionLedger) -> dict[str, Any]:
    value = {
        "session_id": str(uuid.uuid4()),
        "device_id": str(uuid.uuid4()),
        "room_id": f"room-{uuid.uuid4()}",
        "generation": 7,
    }
    ledger.create_session(**value)
    return value


def _terminate_body(session: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "device_id": session["device_id"],
        "room_id": session["room_id"],
        "generation": session["generation"],
        "request_id": request_id,
        "reason": "user_stop",
        "requested_at": "2026-08-24T10:00:00Z",
    }


def _report_all_confirmed(ledger: SessionLedger, session: dict[str, Any],
                          termination_id: str) -> None:
    from app.voice.control_plane import ACK_REPORTERS

    for ack in ACK_NAMES:
        for reporter in ACK_REPORTERS[ack]:
            ledger.record_ack(
                termination_id=termination_id,
                session_id=session["session_id"],
                device_id=session["device_id"],
                room_id=session["room_id"],
                generation=session["generation"],
                acknowledgement=ack,
                reporter=reporter,
                result="confirmed",
            )


def _kws_ready(ledger: SessionLedger) -> dict[str, Any]:
    session = _create_session(ledger)
    accepted = ledger.begin_termination(
        session_id=session["session_id"], generation=session["generation"],
        request_id=str(uuid.uuid4()),
        payload=_terminate_body(session, str(uuid.uuid4())),
    )
    _report_all_confirmed(ledger, session, accepted["termination_id"])
    assert ledger.mark_kws_ready(session["session_id"], session["generation"]) is True
    return session


def _wake_body(session: dict[str, Any], wake_event_id: str) -> dict[str, Any]:
    return {
        "device_id": session["device_id"],
        "prior_session_id": session["session_id"],
        "prior_generation": session["generation"],
        "previous_resource_refs": {
            "session_id": session["session_id"],
            "room_id": session["room_id"],
            "user_sig_fingerprint": hashlib.sha256(b"old-sig").hexdigest(),
        },
        "wake_event_id": wake_event_id,
        "detected_at": "2026-08-25T00:30:00Z",
        "kws_instance_id": "kws-instance-1",
    }


# ---- 1. epoch float 回归守卫 ----


def test_status_response_with_float_terminal_at_is_rejected(
    tmp_path: Path, make_validator
) -> None:
    """terminal 分支若回退为 epoch float，date-time 校验必须失败。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    ledger.finish_termination(accepted["termination_id"], result="partial")

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{accepted['termination_id']}"
    )
    body = resp.json()
    assert body["data"]["terminal_at"] is not None
    forged = dict(body)
    forged["data"] = dict(body["data"])
    forged["data"]["terminal_at"] = 1_758_501_600.0  # 修复前的 epoch float 形态

    with pytest.raises(Exception):
        make_validator("TerminationStatusResponse").validate(forged)


def test_wake_response_with_float_expires_at_is_rejected(
    tmp_path: Path, make_validator
) -> None:
    """expires_at 若回退为 epoch float，date-time 校验必须失败。"""
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(ledger)

    resp = client.post("/api/v1/voice/sessions/wake",
                       json=_wake_body(session, str(uuid.uuid4())))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    forged = dict(body)
    forged["data"] = dict(body["data"])
    forged["data"]["expires_at"] = 1_758_501_600.0

    with pytest.raises(Exception):
        make_validator("WakeSessionResponse").validate(forged)


# ---- 2. retry 40912 端到端 ----


def test_retry_reused_terminate_request_id_returns_40912_matching_contract(
    tmp_path: Path, make_validator
) -> None:
    """retry 复用 root terminate 的 request_id → payload_hash 不同 → 40912；
    响应体必须命中 IdempotencyPayloadMismatchError 分支。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    reused_request_id = str(uuid.uuid4())
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, reused_request_id),
    ).json()["data"]
    parent_tid = accepted["termination_id"]
    ledger.finish_termination(parent_tid, result="timeout")

    mismatch = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{parent_tid}/retry",
        json={"request_id": reused_request_id, "reason": "retry_timeout"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == 40912
    make_validator("IdempotencyPayloadMismatchError").validate(mismatch.json())


# ---- 3. 契约结构守卫 ----


def test_retry_state_conflict_oneof_includes_40912_branch() -> None:
    """RetryTerminationStateConflict.oneOf 必须含 40912 分支，且 40912
    响应体确实命中该分支（防 oneOf 再次遗漏 IdempotencyPayloadMismatchError）。"""
    document = _load_spec()
    conflict = document["components"]["responses"]["RetryTerminationStateConflict"]
    refs = [
        branch["$ref"]
        for branch in conflict["content"]["application/json"]["schema"]["oneOf"]
    ]
    assert "#/components/schemas/IdempotencyPayloadMismatchError" in refs

    mismatch_body = {
        "code": 40912, "data": None, "message": "idempotency_key_payload_mismatch",
    }
    resolver = RefResolver.from_schema(document)
    schema = conflict["content"]["application/json"]["schema"]
    OAS30Validator(schema, resolver=resolver,
                   format_checker=oas30_format_checker).validate(mismatch_body)


def test_error_response_code_50302_maps_to_http_503() -> None:
    """50302 → 503 映射守卫：契约声明 retry 503 TerminationRetryUnavailable；
    error_response 必须将 50302 落到 503 而非 409 兜底。"""
    from app.api.voice_termination_contract import CODE_TO_HTTP, error_response

    assert CODE_TO_HTTP.get(50302) == 503
    resp = error_response(50302)
    assert resp.status_code == 503
    assert resp.body is not None
