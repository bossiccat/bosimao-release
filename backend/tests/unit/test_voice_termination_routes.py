"""商业终止路由 HTTP 契约行为测试。

对冻结契约 docs/api/commercial-voice-openapi.yaml 的三个 Termination 端点做
端到端验证：POST terminate (202)、GET termination status (200)、POST retry (202)。
使用真实 SQLite + SessionLedger + FastAPI TestClient；鉴权用 monkeypatch 绕过
Bearer/nonce/限流（这些由 secured 路由既有测试覆盖），本文件专注 ledger 语义
与错误码映射。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.voice.control_plane import SessionLedger, VoiceStore

ACK_NAMES = (
    "android_trtc_left",
    "sidecar_trtc_left",
    "bridge_drained_closed",
    "apm_cancelled_closed",
    "brain_turns_sealed",
)


def _make_client(tmp_path: Path) -> tuple[TestClient, SessionLedger]:
    """Build the termination router app with a real SQLite store."""
    from app.api.routes_voice_termination import build_termination_router
    from app.voice.rtc_session import RtcSessionConfig, RtcSessionService

    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    ledger = SessionLedger(store)
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


def _terminate_body(session: dict[str, Any], request_id: str,
                    reason: str = "user_stop") -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "device_id": session["device_id"],
        "room_id": session["room_id"],
        "generation": session["generation"],
        "request_id": request_id,
        "reason": reason,
        "requested_at": "2026-08-24T10:00:00Z",
    }


def _report_all(ledger: SessionLedger, session: dict[str, Any],
                termination_id: str, ack: str, result: str = "confirmed") -> None:
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
            result=result,
        )


# ---- POST /terminate ----


def test_terminate_active_session_returns_202_with_contract_shape(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    request_id = str(uuid.uuid4())

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, request_id),
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == ""
    data = body["data"]
    assert data["state"] == "TERMINATING"
    assert data["session_id"] == session["session_id"]
    assert data["generation"] == session["generation"]
    assert "termination_id" in data
    assert "status_url" in data


def test_terminate_same_request_same_payload_is_idempotent_over_http(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    request_id = str(uuid.uuid4())
    body = _terminate_body(session, request_id)

    first = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate", json=body
    )
    second = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate", json=body
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["termination_id"] == second.json()["data"]["termination_id"]


def test_terminate_same_request_different_payload_returns_409(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    request_id = str(uuid.uuid4())

    client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, request_id, reason="user_stop"),
    )
    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, request_id, reason="app_shutdown"),
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == 40912


def test_terminate_already_terminating_different_request_returns_409(tmp_path: Path) -> None:
    client, ledger = _make_client(ledger_path := tmp_path)
    session = _create_session(ledger)
    client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    )
    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == 40916


def test_terminate_unknown_session_returns_404(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    unknown_session = {
        "session_id": str(uuid.uuid4()),
        "device_id": str(uuid.uuid4()),
        "room_id": "room-x",
        "generation": 1,
    }
    resp = client.post(
        f"/api/v1/voice/sessions/{unknown_session['session_id']}/terminate",
        json=_terminate_body(unknown_session, str(uuid.uuid4())),
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_terminate_context_mismatch_returns_409(tmp_path: Path) -> None:
    """session_id/device_id/room_id/generation 不一致拒绝。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    bad_body = _terminate_body(session, str(uuid.uuid4()))
    bad_body["device_id"] = str(uuid.uuid4())  # mismatched device

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate", json=bad_body
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == 40916


def test_terminate_body_session_id_must_match_path(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    body = _terminate_body(session, str(uuid.uuid4()))
    body["session_id"] = str(uuid.uuid4())  # different from path

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate", json=body
    )

    assert resp.status_code in (400, 409)
    assert resp.json()["code"] in (40001, 40916)


def test_terminate_invalid_reason_returns_422(tmp_path: Path) -> None:
    """reason 不在契约 enum 内 → FastAPI 校验 422。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    body = _terminate_body(session, str(uuid.uuid4()), reason="not_in_enum")

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate", json=body
    )

    assert resp.status_code == 422


# ---- GET termination status ----


def test_get_pending_termination_returns_root_pending_variant(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}"
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status_variant"] == "root_pending"
    assert data["type"] == "session.terminating"
    assert data["scope"] == "root"
    assert data["result"] == "pending"
    assert data["terminal_at"] is None
    assert data["retryable"] is False
    assert set(data["acknowledgements"].keys()) == set(ACK_NAMES)
    assert all(v == "pending" for v in data["acknowledgements"].values())


def test_get_complete_termination_returns_root_complete_variant(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]

    for ack in ACK_NAMES:
        _report_all(ledger, session, tid, ack, "confirmed")

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}"
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status_variant"] == "root_complete"
    assert data["type"] == "session.terminated"
    assert data["result"] == "complete"
    assert data["terminal_at"] is not None
    assert data["retryable"] is False
    assert all(v == "confirmed" for v in data["acknowledgements"].values())


def test_get_partial_termination_returns_root_partial_retryable(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]
    ledger.finish_termination(tid, result="partial")

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}"
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status_variant"] == "root_partial"
    assert data["result"] == "partial"
    assert data["retryable"] is True
    assert data["terminal_at"] is not None


def test_get_unknown_termination_returns_404(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{uuid.uuid4()}"
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == 40403


# ---- POST /retry ----


def test_retry_partial_parent_returns_202_with_child_shape(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    parent_tid = accepted["termination_id"]
    ledger.finish_termination(parent_tid, result="partial")

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{parent_tid}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["termination_id"] != parent_tid
    assert data["parent_termination_id"] == parent_tid
    assert data["state"] == "TERMINATING"
    assert data["session_id"] == session["session_id"]


def test_retry_not_partial_or_timeout_returns_409(tmp_path: Path) -> None:
    """pending 父不可 retry。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]  # still pending

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == 40913


def test_retry_unknown_parent_returns_404(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{uuid.uuid4()}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == 40403


def test_retry_invalid_reason_returns_422(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    tid = accepted["termination_id"]
    ledger.finish_termination(tid, result="partial")

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}/retry",
        json={"request_id": str(uuid.uuid4()), "reason": "invalid_reason"},
    )

    assert resp.status_code == 422


def test_retry_child_status_returns_retry_child_pending_variant(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    parent_tid = accepted["termination_id"]
    ledger.finish_termination(parent_tid, result="timeout")

    child = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{parent_tid}/retry",
        json={"request_id": str(uuid.uuid4()), "reason": "retry_timeout"},
    ).json()["data"]

    resp = client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{child['termination_id']}"
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status_variant"] == "retry_child_pending"
    assert data["scope"] == "retry_child"
    assert data["parent_termination_id"] == parent_tid
    assert data["result"] == "pending"


# ---- POST .../acknowledgements (ack report endpoint) ----


def _make_ack_client(tmp_path: Path, reporter: str) -> tuple[TestClient, SessionLedger]:
    """Termination router with a fixed reporter resolver (credential-derived)."""
    from app.api.routes_voice_termination import build_termination_router

    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    ledger = SessionLedger(store)
    app = FastAPI()
    app.include_router(
        build_termination_router(ledger=ledger, reporter_resolver=lambda request: reporter)
    )
    return TestClient(app), ledger


def _ack_body(session: dict[str, Any], ack: str, result: str = "confirmed",
              error_code: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "acknowledgement": ack,
        "result": result,
        "session_id": session["session_id"],
        "device_id": session["device_id"],
        "room_id": session["room_id"],
        "generation": session["generation"],
        "reported_at": "2026-08-24T10:00:00Z",
    }
    if error_code is not None:
        body["error_code"] = error_code
    return body


def _begin_termination(client: TestClient, session: dict[str, Any]) -> str:
    accepted = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    ).json()["data"]
    return accepted["termination_id"]


def _ack_url(session: dict[str, Any], termination_id: str) -> str:
    return (f"/api/v1/voice/sessions/{session['session_id']}"
            f"/termination/{termination_id}/acknowledgements")


def test_ack_report_returns_202_with_contract_shape(tmp_path: Path) -> None:
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "android_trtc_left"))

    assert resp.status_code == 202
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == ""
    data = body["data"]
    assert data["termination_id"] == tid
    assert data["session_id"] == session["session_id"]
    assert data["generation"] == session["generation"]
    assert data["acknowledgement"] == "android_trtc_left"
    assert data["ack_result"] == "confirmed"
    assert data["result"] == "pending"
    assert data["status_url"].endswith(f"/termination/{tid}")


def test_ack_report_by_unauthorized_reporter_returns_40901(tmp_path: Path) -> None:
    """sidecar credential (sidecar reporter) cannot report android_trtc_left."""
    client, ledger = _make_ack_client(tmp_path, "sidecar")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "android_trtc_left"))

    assert resp.status_code == 409
    assert resp.json()["code"] == 40901
    # nothing recorded
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["android_trtc_left"] == "pending"


def test_ack_report_unknown_ack_returns_422(tmp_path: Path) -> None:
    """acknowledgement 枚举外值由 schema 校验拒绝（422，与 retry reason 一致）。"""
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "not_an_ack"))

    assert resp.status_code == 422


def test_ack_report_invalid_result_value_returns_422(tmp_path: Path) -> None:
    """result 只接受 confirmed/failed；pending/timed_out 是 CP 聚合产物，不可上报。"""
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "android_trtc_left", result="pending"))

    assert resp.status_code == 422


def test_ack_report_context_mismatch_returns_40901(tmp_path: Path) -> None:
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    wrong = dict(session, room_id="room-not-the-real-one")
    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(wrong, "android_trtc_left"))

    assert resp.status_code == 409
    assert resp.json()["code"] == 40901


def test_ack_report_after_terminal_returns_40901(tmp_path: Path) -> None:
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)
    ledger.finish_termination(tid, result="timeout")

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "android_trtc_left"))

    assert resp.status_code == 409
    assert resp.json()["code"] == 40901


def test_ack_report_failed_result_requires_error_code_over_http(tmp_path: Path) -> None:
    """failed 报告必须携带脱敏 error_code（契约 AckReportRequest 跨字段约束 → 422）。"""
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "android_trtc_left", result="failed"))

    assert resp.status_code == 422


def test_ack_report_confirmed_with_error_code_returns_422(tmp_path: Path) -> None:
    """confirmed 报告不得携带 error_code。"""
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    resp = client.post(_ack_url(session, tid),
                       json=_ack_body(session, "android_trtc_left",
                                      error_code="SHOULD_NOT_BE_HERE"))

    assert resp.status_code == 422


def test_ack_report_path_session_mismatch_returns_40403(tmp_path: Path) -> None:
    """路径 session 与 body session 不同 → termination_not_found 语义。"""
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    wrong_body = _ack_body(session, "android_trtc_left")
    wrong_body["session_id"] = str(uuid.uuid4())
    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}"
        f"/termination/{tid}/acknowledgements",
        json=wrong_body,
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == 40403


def test_ack_report_unknown_termination_returns_40403(tmp_path: Path) -> None:
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)

    resp = client.post(_ack_url(session, str(uuid.uuid4())),
                       json=_ack_body(session, "android_trtc_left"))

    assert resp.status_code == 404
    assert resp.json()["code"] == 40403


def test_ack_report_bridge_dual_report_aggregates_over_http(tmp_path: Path) -> None:
    """bridge_drained_closed 走 HTTP 双报告：sidecar 单报 pending，rtc_bridge 补报 confirmed。"""
    client, ledger = _make_ack_client(tmp_path, "sidecar")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    first = client.post(_ack_url(session, tid),
                        json=_ack_body(session, "bridge_drained_closed"))
    assert first.status_code == 202
    assert first.json()["data"]["ack_result"] == "pending"
    assert first.json()["data"]["result"] == "pending"

    # switch credential to rtc_bridge via direct ledger call would bypass HTTP;
    # rebuild client is overkill — use the resolver-free path: sidecar reporter
    # is one of the two authorized, rtc_bridge report goes through a second app.
    client2, _ = _make_ack_client(tmp_path, "rtc_bridge")
    second = client2.post(_ack_url(session, tid),
                          json=_ack_body(session, "bridge_drained_closed"))
    assert second.status_code == 202
    data = second.json()["data"]
    assert data["ack_result"] == "confirmed"
    assert data["result"] == "pending"  # other acks still missing


def test_ack_report_last_confirmation_completes_termination_over_http(tmp_path: Path) -> None:
    """最后一项聚合 confirmed 时 result 变为 complete。"""
    client, ledger = _make_ack_client(tmp_path, "sidecar")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    for ack in ACK_NAMES:
        from app.voice.control_plane import ACK_REPORTERS
        for reporter in ACK_REPORTERS[ack]:
            c, _ = _make_ack_client(tmp_path, reporter)
            resp = c.post(_ack_url(session, tid), json=_ack_body(session, ack))
            assert resp.status_code == 202, resp.text

    status = ledger.get_termination(tid)
    assert status["result"] == "complete"
    assert status["state"] == "TERMINATED"


def test_ack_report_extra_field_returns_422(tmp_path: Path) -> None:
    """additionalProperties: false — reporter 字段出现在 body 中必须整体拒绝。"""
    client, ledger = _make_ack_client(tmp_path, "android")
    session = _create_session(ledger)
    tid = _begin_termination(client, session)

    body = _ack_body(session, "android_trtc_left")
    body["reporter"] = "android"  # client self-reported reporter
    resp = client.post(_ack_url(session, tid), json=body)

    assert resp.status_code == 422


# ---- POST /sessions/wake ----


def _wake_body(session: dict[str, Any], wake_event_id: str,
               *, room_id: str | None = None) -> dict[str, Any]:
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
        "_room_hint": room_id,
    }


def _kws_ready(client: TestClient, ledger: SessionLedger) -> dict[str, Any]:
    session = _create_session(ledger)
    tid = _begin_termination(client, session)
    _report_all(ledger, session, tid, "android_trtc_left")
    _report_all(ledger, session, tid, "sidecar_trtc_left")
    _report_all(ledger, session, tid, "bridge_drained_closed")
    _report_all(ledger, session, tid, "apm_cancelled_closed")
    _report_all(ledger, session, tid, "brain_turns_sealed")
    assert ledger.mark_kws_ready(session["session_id"], session["generation"]) is True
    return session


def test_wake_returns_201_with_fresh_resources(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)
    body = _wake_body(session, str(uuid.uuid4()))
    room_hint = body.pop("_room_hint")

    resp = client.post("/api/v1/voice/sessions/wake", json=body)

    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["prior_generation"] == session["generation"]
    assert data["generation"] == session["generation"] + 1
    assert data["generation_transition"] == {
        "from_generation": session["generation"],
        "to_generation": session["generation"] + 1,
        "increment": 1,
    }
    assert data["resource_freshness"] == {
        "session_id_changed": True,
        "room_id_changed": True,
        "user_sig_changed": True,
    }
    assert data["session_id"] != session["session_id"]
    assert data["room_id"] != session["room_id"]
    assert data["scene"] == "trtc_full_duplex"
    assert "user_sig" in data and data["user_sig"]
    assert "expires_at" in data


def test_wake_replay_same_request_returns_original(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)
    body = _wake_body(session, str(uuid.uuid4()))
    body.pop("_room_hint")

    first = client.post("/api/v1/voice/sessions/wake", json=body)
    replay = client.post("/api/v1/voice/sessions/wake", json=body)

    assert first.status_code == replay.status_code == 201
    assert replay.json()["data"] == first.json()["data"]


def test_wake_not_kws_ready_returns_409(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)  # ACTIVE
    body = _wake_body(session, str(uuid.uuid4()))
    body.pop("_room_hint")

    resp = client.post("/api/v1/voice/sessions/wake", json=body)

    assert resp.status_code == 409
    assert resp.json()["code"] == 40917


def test_wake_generation_mismatch_returns_40917(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)
    body = _wake_body(session, str(uuid.uuid4()))
    body.pop("_room_hint")
    body["prior_generation"] = session["generation"] - 1

    resp = client.post("/api/v1/voice/sessions/wake", json=body)

    assert resp.status_code == 409
    assert resp.json()["code"] == 40917


def test_wake_unknown_prior_session_returns_404(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)
    body = _wake_body(session, str(uuid.uuid4()))
    body.pop("_room_hint")
    body["prior_session_id"] = str(uuid.uuid4())
    body["previous_resource_refs"]["session_id"] = body["prior_session_id"]

    resp = client.post("/api/v1/voice/sessions/wake", json=body)

    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_wake_prior_refs_mismatch_returns_422(tmp_path: Path) -> None:
    """x-field-constraints: prior_session_id == previous_resource_refs.session_id。"""
    client, ledger = _make_client(tmp_path)
    session = _kws_ready(client, ledger)
    body = _wake_body(session, str(uuid.uuid4()))
    body.pop("_room_hint")
    body["previous_resource_refs"]["session_id"] = str(uuid.uuid4())

    resp = client.post("/api/v1/voice/sessions/wake", json=body)

    assert resp.status_code == 422


# ---- POST /sessions/{session_id}/kws-ready ----


def _kws_ready_body(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "device_id": session["device_id"],
        "generation": session["generation"],
        "evidence": {"kws_model": "sherpa-onnx", "armed_at": "2026-08-29T02:00:00Z"},
    }


def _terminated_session(client: TestClient, ledger: SessionLedger) -> dict[str, Any]:
    """ACTIVE → terminate → 五 ack 全确认 → TERMINATED（未标 KWS_READY）。"""
    session = _create_session(ledger)
    tid = _begin_termination(client, session)
    _report_all(ledger, session, tid, "android_trtc_left")
    _report_all(ledger, session, tid, "sidecar_trtc_left")
    _report_all(ledger, session, tid, "bridge_drained_closed")
    _report_all(ledger, session, tid, "apm_cancelled_closed")
    _report_all(ledger, session, tid, "brain_turns_sealed")
    return session


def test_kws_ready_on_terminated_session_returns_201(tmp_path: Path) -> None:
    """TERMINATED(complete) 会话上报 KWS 就绪 → KWS_READY，契约形状锁定。"""
    client, ledger = _make_client(tmp_path)
    session = _terminated_session(client, ledger)

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/kws-ready",
        json=_kws_ready_body(session),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == ""
    data = body["data"]
    assert data["session_id"] == session["session_id"]
    assert data["generation"] == session["generation"]
    assert data["state"] == "KWS_READY"
    assert data["kws_ready"] is True
    recorded = ledger.get_kws_readiness(session["session_id"], session["generation"])
    assert recorded and recorded[0]["reporter"] == "android"


def test_kws_ready_on_active_session_returns_40917(tmp_path: Path) -> None:
    """未完成终止的 ACTIVE 会话不得进入 KWS_READY。"""
    client, ledger = _make_client(tmp_path)
    session = _create_session(ledger)

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/kws-ready",
        json=_kws_ready_body(session),
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == 40917


def test_kws_ready_unknown_session_returns_40402(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    body = _kws_ready_body({
        "session_id": str(uuid.uuid4()), "device_id": str(uuid.uuid4()),
        "generation": 0,
    })

    resp = client.post(
        f"/api/v1/voice/sessions/{body['session_id']}/kws-ready", json=body,
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_kws_ready_generation_mismatch_returns_40917(tmp_path: Path) -> None:
    client, ledger = _make_client(tmp_path)
    session = _terminated_session(client, ledger)
    body = _kws_ready_body(session)
    body["generation"] = session["generation"] + 5

    resp = client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/kws-ready",
        json=body,
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == 40917
