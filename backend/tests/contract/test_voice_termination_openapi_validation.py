"""商业终止/唤醒端点 HTTP 响应体 OpenAPI 契约校验。

对冻结契约 docs/api/commercial-voice-openapi.yaml 的四个核心端点
（terminate 202 / termination status 200 / retry 202 / wake 201）发起真实
TestClient 请求，并将响应体逐字段过 OAS30Validator —— 确认 ISO8601
date-time 序列化（terminal_at / expires_at）与 OpenAPI 声明的 format 一致，
防止 REAL epoch 数字再次漏出到 HTTP 边界。

鉴权绕过方式与 test_voice_termination_routes 相同（secured 层由既有测试覆盖）。
"""
from __future__ import annotations

import hashlib
import uuid
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

# wake 路径要求注入 userSig 加密器（未注入 → fail-closed 拒绝签发）
_TEST_USER_SIG_CIPHER = UserSigCipher(
    hashlib.sha256(b"task-b-user-sig-cipher-test-key").digest()
)

ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = ROOT / "docs" / "api" / "commercial-voice-openapi.yaml"

ACK_NAMES = (
    "android_trtc_left",
    "sidecar_trtc_left",
    "bridge_drained_closed",
    "apm_cancelled_closed",
    "brain_turns_sealed",
)


@pytest.fixture(scope="module")
def spec() -> dict:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def validator_factory(spec: dict):
    def _make(schema_name: str) -> OAS30Validator:
        resolver = RefResolver.from_schema(spec)
        schema = {"$ref": f"#/components/schemas/{schema_name}"}
        return OAS30Validator(schema, resolver=resolver,
                              format_checker=oas30_format_checker)
    return _make


def _make_client(tmp_path: Path) -> tuple[TestClient, SessionLedger]:
    from app.api.routes_voice_termination import build_termination_router
    from app.voice.rtc_session import RtcSessionConfig, RtcSessionService

    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    ledger = SessionLedger(store, user_sig_cipher=_TEST_USER_SIG_CIPHER)
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


def _report_all(ledger: SessionLedger, session: dict[str, Any],
                termination_id: str, ack: str) -> None:
    from app.voice.control_plane import ACK_REPORTERS

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


def _kws_ready(client: TestClient, ledger: SessionLedger) -> dict[str, Any]:
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]
    for ack in ACK_NAMES:
        _report_all(ledger, session, tid, ack)
    assert ledger.mark_kws_ready(session["session_id"], session["generation"]) is True
    return session


def _wake_body(session: dict[str, Any], wake_event_id: str) -> dict[str, Any]:
    import hashlib
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


def _assert_valid(validator: OAS30Validator, body: dict, label: str) -> None:
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{label} 响应体不符合 OpenAPI 契约: "
        + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
    )


# ---- POST /terminate 202 ----


def test_terminate_response_validates_against_openapi(
    tmp_path: Path, validator_factory
) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    )

    assert resp.status_code == 202
    _assert_valid(validator_factory("TerminationAcceptedResponse"),
                  resp.json(), "terminate 202")


# ---- GET /termination/{id} 200（pending 与 terminal 两个变体都校验 terminal_at 格式）----


def test_termination_status_pending_validates_against_openapi(
    tmp_path: Path, validator_factory
) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{accepted['termination_id']}"
    )

    assert resp.status_code == 200
    body = resp.json()
    # terminal_at 为 null 时必须可被 date-time nullable 契约接受
    assert body["data"]["terminal_at"] is None
    _assert_valid(validator_factory("TerminationStatusResponse"),
                  body, "status pending 200")


def test_termination_status_complete_validates_terminal_at_iso8601(
    tmp_path: Path, validator_factory
) -> None:
    """complete 变体的 terminal_at 必须是 date-time 字符串，不是 epoch 数字。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]
    for ack in ACK_NAMES:
        _report_all(ledger, session, tid, ack)

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"]["terminal_at"], str)  # 不是 float
    _assert_valid(validator_factory("TerminationStatusResponse"),
                  body, "status complete 200")


# ---- POST /retry 202 ----


def test_retry_response_validates_against_openapi(
    tmp_path: Path, validator_factory
) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    parent_tid = accepted["termination_id"]
    ledger.finish_termination(parent_tid, result="partial")

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{parent_tid}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
    )

    assert resp.status_code == 202
    _assert_valid(validator_factory("RetryTerminationAcceptedResponse"),
                  resp.json(), "retry 202")


def test_retry_child_status_validates_terminal_at_iso8601(
    tmp_path: Path, validator_factory
) -> None:
    """retry_child_partial 变体的 terminal_at 必须是 date-time 字符串。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    parent_tid = accepted["termination_id"]
    ledger.finish_termination(parent_tid, result="partial")

    child = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{parent_tid}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
    ).json()["data"]
    ledger.finish_termination(child["termination_id"], result="partial")

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{child['termination_id']}"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status_variant"] == "retry_child_partial"
    assert isinstance(body["data"]["terminal_at"], str)  # 不是 float
    _assert_valid(validator_factory("TerminationStatusResponse"),
                  body, "retry child status 200")


# ---- POST /sessions/wake 201 ----


def test_wake_response_validates_against_openapi(
    tmp_path: Path, validator_factory
) -> None:
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)

    resp = client.post("/api/v1/voice/sessions/wake",
                       json=_wake_body(session, str(uuid.uuid4())))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    # expires_at 必须是 date-time 字符串，不是 epoch 数字
    assert isinstance(body["data"]["expires_at"], str)
    _assert_valid(validator_factory("WakeSessionResponse"), body, "wake 201")


def test_wake_replay_response_validates_against_openapi(
    tmp_path: Path, validator_factory
) -> None:
    """重放返回原始响应；expires_at 也必须保持 date-time。"""
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)
    body = _wake_body(session, str(uuid.uuid4()))

    first = client.post("/api/v1/voice/sessions/wake", json=body)
    replay = client.post("/api/v1/voice/sessions/wake", json=body)

    assert first.status_code == replay.status_code == 201
    _assert_valid(validator_factory("WakeSessionResponse"),
                  replay.json(), "wake replay 201")


# 负向对照与 40912 分支守卫见 test_voice_openapi_regression_guards.py
# （本文件已接近 300 行门禁，按代码组织规范拆分）。
