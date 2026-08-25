"""终止端点组合层鉴权行为测试（契约 security 定义）。

使用真实 FastAPI/SQLite fixture（create_secured_voice_router 已挂载 termination
router + guard）。验证：
- terminate/retry 仅 device Bearer + nonce；无 Bearer 401、sidecar 拒绝、nonce 缺失 401
- termination_status 允许 device 或 sidecar Bearer（只读，无 nonce）
- 认证通过后全链路（terminate → status → retry）可用
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from .voice_security_fixture import (
    BRAIN_SECRET,
    DEVICE_A,
    OWNER_SECRET,
    RTC_BRIDGE_SECRET,
    SECRET_A,
    SIDECAR_SECRET,
    VoiceSecurityFixture,
    nonce,
)


@pytest.fixture()
def fx(tmp_path):
    return VoiceSecurityFixture(tmp_path)


def _register_session(fx) -> dict[str, Any]:
    """在 ledger 中直接登记一个 ACTIVE 会话（经由 store 之上的 SessionLedger）。"""
    from app.voice.control_plane import SessionLedger

    ledger = SessionLedger(fx.store)
    session = {
        "session_id": str(uuid.uuid4()),
        "device_id": DEVICE_A,
        "room_id": f"room-{uuid.uuid4().hex[:8]}",
        "generation": 7,
    }
    ledger.create_session(**session)
    return session


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


def _device_terminate(fx, session: dict[str, Any],
                      request_id: str | None = None) -> Any:
    return fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, request_id or str(uuid.uuid4())),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()),
    )


# ---- terminate 鉴权 ----


def test_terminate_without_bearer_returns_401(fx) -> None:
    session = _register_session(fx)
    resp = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_terminate_with_sidecar_bearer_rejected(fx) -> None:
    """契约：terminate 仅 device credential 可调用。"""
    session = _register_session(fx)
    resp = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
        headers={
            "Authorization": f"Bearer {SIDECAR_SECRET}",
            "X-Request-Nonce": nonce(),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_terminate_without_nonce_rejected(fx) -> None:
    session = _register_session(fx)
    resp = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
        headers=fx.auth_headers(DEVICE_A, SECRET_A),  # no nonce
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40102


def test_terminate_with_valid_device_bearer_returns_202(fx) -> None:
    session = _register_session(fx)
    resp = _device_terminate(fx, session)
    assert resp.status_code == 202
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["state"] == "TERMINATING"


def test_terminate_nonce_replay_rejected(fx) -> None:
    """同一 nonce 二次消费必须拒绝（40102）。"""
    session = _register_session(fx)
    shared_nonce = nonce()
    first = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, shared_nonce),
    )
    assert first.status_code == 202
    replay = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=_terminate_body(session, str(uuid.uuid4())),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, shared_nonce),
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == 40102


# ---- termination_status 鉴权 ----


def test_status_without_bearer_returns_401(fx) -> None:
    session = _register_session(fx)
    resp = fx.client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{uuid.uuid4()}"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_status_with_device_bearer_works_without_nonce(fx) -> None:
    """status 是只读端点：device Bearer 即可，无需 nonce。"""
    session = _register_session(fx)
    accepted = _device_terminate(fx, session).json()["data"]
    resp = fx.client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination"
        f"/{accepted['termination_id']}",
        headers=fx.auth_headers(DEVICE_A, SECRET_A),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status_variant"] == "root_pending"


def test_status_with_sidecar_bearer_allowed(fx) -> None:
    """契约：termination_status 允许 device 或 sidecar Bearer。"""
    session = _register_session(fx)
    accepted = _device_terminate(fx, session).json()["data"]
    resp = fx.client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination"
        f"/{accepted['termination_id']}",
        headers={"Authorization": f"Bearer {SIDECAR_SECRET}"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status_variant"] == "root_pending"


# ---- retry 鉴权 ----


def test_retry_with_sidecar_bearer_rejected(fx) -> None:
    """契约：retry 仅 device credential 可调用。"""
    session = _register_session(fx)
    accepted = _device_terminate(fx, session).json()["data"]
    tid = accepted["termination_id"]
    # force partial via ledger
    from app.voice.control_plane import SessionLedger

    SessionLedger(fx.store).finish_termination(tid, result="partial")

    resp = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{tid}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
        headers={
            "Authorization": f"Bearer {SIDECAR_SECRET}",
            "X-Request-Nonce": nonce(),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_full_device_flow_terminate_status_retry_over_secured_router(fx) -> None:
    """认证后的完整链路：terminate → (partial) → status → retry → child status。"""
    from app.voice.control_plane import SessionLedger

    ledger = SessionLedger(fx.store)
    session = _register_session(fx)

    # 1. terminate（device 认证）
    accepted = _device_terminate(fx, session).json()["data"]
    parent_tid = accepted["termination_id"]

    # 2. 父周期 finish partial（sidecar/内部进程在真实链路触发）
    ledger.finish_termination(parent_tid, result="partial")

    # 3. status（device 认证，只读无 nonce）
    status = fx.client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{parent_tid}",
        headers=fx.auth_headers(DEVICE_A, SECRET_A),
    )
    assert status.status_code == 200
    assert status.json()["data"]["status_variant"] == "root_partial"
    assert status.json()["data"]["retryable"] is True

    # 4. retry（device 认证 + nonce）
    retry = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/termination/{parent_tid}/retry",
        json={"request_id": str(uuid.uuid4()),
              "reason": "retry_failed_acknowledgements"},
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()),
    )
    assert retry.status_code == 202
    child = retry.json()["data"]
    assert child["parent_termination_id"] == parent_tid

    # 5. child status（sidecar 也可读）
    child_status = fx.client.get(
        f"/api/v1/voice/sessions/{session['session_id']}/termination"
        f"/{child['termination_id']}",
        headers={"Authorization": f"Bearer {SIDECAR_SECRET}"},
    )
    assert child_status.status_code == 200
    assert child_status.json()["data"]["status_variant"] == "retry_child_pending"


def test_wrong_device_cannot_terminate_others_session(fx) -> None:
    """device B 的凭证不能终止 device A 的会话。

    会话登记在 device A 名下；device B 的 Bearer 合法（guard 放行），但
    body.device_id=B 与会话记录的 device_id=A 不一致 → ledger 上下文校验
    拒绝 (40916)。
    """
    from .voice_security_fixture import DEVICE_B, SECRET_B

    session = _register_session(fx)  # device A 的会话
    body = _terminate_body(session, str(uuid.uuid4()))
    body["device_id"] = DEVICE_B  # device B 声称是自己的会话
    resp = fx.client.post(
        f"/api/v1/voice/sessions/{session['session_id']}/terminate",
        json=body,
        headers=fx.auth_headers(DEVICE_B, SECRET_B, nonce()),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40916


# ---- ack 上报端点鉴权（契约：四类主体 + reporter 由 credential 派生） ----


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


def _ack_url(session: dict[str, Any], tid: str) -> str:
    return (f"/api/v1/voice/sessions/{session['session_id']}"
            f"/termination/{tid}/acknowledgements")


def _begin_tid(fx, session: dict[str, Any]) -> str:
    accepted = _device_terminate(fx, session).json()["data"]
    return accepted["termination_id"]


def test_ack_without_bearer_returns_401(fx) -> None:
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(_ack_url(session, tid),
                          json=_ack_body(session, "android_trtc_left"))
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_ack_without_nonce_rejected(fx) -> None:
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "android_trtc_left"),
        headers=fx.auth_headers(DEVICE_A, SECRET_A),  # no nonce
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40102


def test_ack_device_reports_android_trtc_left(fx) -> None:
    """device credential 派生 reporter=android，可报 android_trtc_left。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "android_trtc_left"),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()),
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["acknowledgement"] == "android_trtc_left"
    assert data["ack_result"] == "confirmed"
    assert data["result"] == "pending"


def test_ack_device_cannot_report_brain_turns_sealed(fx) -> None:
    """device（reporter=android）报 brain_turns_sealed → 40901，未记账。"""
    from app.voice.control_plane import SessionLedger

    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "brain_turns_sealed"),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40901
    ledger = SessionLedger(fx.store)
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["brain_turns_sealed"] == "pending"


def test_ack_sidecar_reports_bridge_drained_partial(fx) -> None:
    """sidecar credential 报 bridge_drained_closed：单份报告聚合仍 pending。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "bridge_drained_closed"),
        headers=fx.service_headers(SIDECAR_SECRET, nonce()),
    )
    assert resp.status_code == 202
    assert resp.json()["data"]["ack_result"] == "pending"


def test_ack_rtc_bridge_reports_apm_cancelled(fx) -> None:
    """rtc_bridge service credential 派生 reporter=rtc_bridge，可报 A4。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "apm_cancelled_closed", result="failed",
                       error_code="APM_UPSTREAM_STUCK"),
        headers=fx.service_headers(RTC_BRIDGE_SECRET, nonce()),
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["ack_result"] == "failed"
    assert data["result"] == "pending"


def test_ack_brain_reports_brain_turns_sealed(fx) -> None:
    """brain service credential 派生 reporter=brain，可报 brain_turns_sealed。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "brain_turns_sealed"),
        headers=fx.service_headers(BRAIN_SECRET, nonce()),
    )
    assert resp.status_code == 202
    assert resp.json()["data"]["ack_result"] == "confirmed"


def test_ack_rtc_bridge_cannot_report_android_ack(fx) -> None:
    """rtc_bridge（reporter=rtc_bridge）报 android_trtc_left → 40901。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "android_trtc_left"),
        headers=fx.service_headers(RTC_BRIDGE_SECRET, nonce()),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40901


def test_ack_unknown_service_token_rejected(fx) -> None:
    """未知服务 token（既非四类主体）→ 40101。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "android_trtc_left"),
        headers=fx.service_headers("totally-unknown-secret-0123456789", nonce()),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_ack_owner_bearer_not_accepted(fx) -> None:
    """owner 主体未被契约授权上报 ack → 40101。"""
    session = _register_session(fx)
    tid = _begin_tid(fx, session)
    resp = fx.client.post(
        _ack_url(session, tid),
        json=_ack_body(session, "android_trtc_left"),
        headers=fx.service_headers(OWNER_SECRET, nonce()),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_ack_full_cycle_completes_via_four_principals(fx) -> None:
    """四类主体经真实 HTTP 完成五项确认 → 终止 complete。"""
    from app.voice.control_plane import SessionLedger

    session = _register_session(fx)
    tid = _begin_tid(fx, session)

    def _post(ack: str, headers: dict) -> None:
        resp = fx.client.post(_ack_url(session, tid), json=_ack_body(session, ack),
                              headers=headers)
        assert resp.status_code == 202, resp.text

    _post("android_trtc_left", fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    _post("sidecar_trtc_left", fx.service_headers(SIDECAR_SECRET, nonce()))
    _post("bridge_drained_closed", fx.service_headers(SIDECAR_SECRET, nonce()))
    _post("bridge_drained_closed", fx.service_headers(RTC_BRIDGE_SECRET, nonce()))
    _post("apm_cancelled_closed", fx.service_headers(RTC_BRIDGE_SECRET, nonce()))
    last = fx.client.post(_ack_url(session, tid),
                          json=_ack_body(session, "brain_turns_sealed"),
                          headers=fx.service_headers(BRAIN_SECRET, nonce()))
    assert last.status_code == 202
    assert last.json()["data"]["result"] == "complete"

    ledger = SessionLedger(fx.store)
    status = ledger.get_termination(tid)
    assert status["state"] == "TERMINATED"
    assert status["result"] == "complete"


# ---- wake 端点鉴权（契约：仅 device Bearer + nonce；KWS_READY → SIGNING(n+1)） ----


def _wake_body(session: dict[str, Any], wake_event_id: str,
               *, detected_at: str = "2026-08-25T00:30:00Z") -> dict[str, Any]:
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
        "detected_at": detected_at,
        "kws_instance_id": "kws-instance-1",
    }


def _kws_ready(fx) -> dict[str, Any]:
    """真实全链路推到 KWS_READY：登记 → HTTP 终止 → 四类主体 HTTP 五项 ack →
    mark_kws_ready（ readiness 转换由内部进程触发，无 HTTP 端点，直接走 ledger）。"""
    from app.voice.control_plane import SessionLedger

    session = _register_session(fx)
    tid = _begin_tid(fx, session)

    def _post(ack: str, headers: dict) -> None:
        resp = fx.client.post(_ack_url(session, tid), json=_ack_body(session, ack),
                              headers=headers)
        assert resp.status_code == 202, resp.text

    _post("android_trtc_left", fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    _post("sidecar_trtc_left", fx.service_headers(SIDECAR_SECRET, nonce()))
    _post("bridge_drained_closed", fx.service_headers(SIDECAR_SECRET, nonce()))
    _post("bridge_drained_closed", fx.service_headers(RTC_BRIDGE_SECRET, nonce()))
    _post("apm_cancelled_closed", fx.service_headers(RTC_BRIDGE_SECRET, nonce()))
    _post("brain_turns_sealed", fx.service_headers(BRAIN_SECRET, nonce()))
    assert SessionLedger(fx.store).mark_kws_ready(
        session["session_id"], session["generation"]) is True
    return session


def test_wake_without_bearer_returns_401(fx) -> None:
    session = _register_session(fx)
    resp = fx.client.post("/api/v1/voice/sessions/wake",
                          json=_wake_body(session, str(uuid.uuid4())))
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_wake_with_sidecar_bearer_rejected(fx) -> None:
    """契约：wake 仅 device credential 可调用。"""
    session = _register_session(fx)
    resp = fx.client.post("/api/v1/voice/sessions/wake",
                          json=_wake_body(session, str(uuid.uuid4())),
                          headers=fx.service_headers(SIDECAR_SECRET, nonce()))
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_wake_without_nonce_rejected(fx) -> None:
    session = _register_session(fx)
    resp = fx.client.post("/api/v1/voice/sessions/wake",
                          json=_wake_body(session, str(uuid.uuid4())),
                          headers=fx.auth_headers(DEVICE_A, SECRET_A))  # no nonce
    assert resp.status_code == 401
    assert resp.json()["code"] == 40102


def test_wake_nonce_replay_rejected(fx) -> None:
    """同一 nonce 二次消费必须拒绝（40102），与 terminate 行为一致。

    第一请求虽被 ledger 拒绝（409 非 KWS_READY），但 guard 已原子消费 nonce；
    重放该 nonce 的第二请求必须在鉴权层被挡（40102）。
    """
    session = _register_session(fx)
    shared_nonce = nonce()
    body = _wake_body(session, str(uuid.uuid4()))
    first = fx.client.post("/api/v1/voice/sessions/wake", json=body,
                           headers=fx.auth_headers(DEVICE_A, SECRET_A, shared_nonce))
    assert first.status_code == 409  # ACTIVE 会话 → ledger 语义拒绝（nonce 已消费）
    replay = fx.client.post("/api/v1/voice/sessions/wake", json=body,
                            headers=fx.auth_headers(DEVICE_A, SECRET_A, shared_nonce))
    assert replay.status_code == 401
    assert replay.json()["code"] == 40102


def test_wake_not_kws_ready_returns_40917(fx) -> None:
    """ACTIVE 会话（未终止）wake → 40917 stale_or_conflicting_generation。"""
    session = _register_session(fx)  # ACTIVE
    resp = fx.client.post("/api/v1/voice/sessions/wake",
                          json=_wake_body(session, str(uuid.uuid4())),
                          headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    assert resp.status_code == 409
    assert resp.json()["code"] == 40917


def test_wake_success_returns_201_with_fresh_resources(fx) -> None:
    """认证后的完整链路：terminate → 四主体 ack → KWS_READY → wake 201。

    契约 §5.1：generation=n+1；session_id/room_id/user_sig 全新；
    resource_freshness 三项 changed 全 true；scene=trtc_full_duplex。
    """
    session = _kws_ready(fx)
    resp = fx.client.post("/api/v1/voice/sessions/wake",
                          json=_wake_body(session, str(uuid.uuid4())),
                          headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["prior_session_id"] == session["session_id"]
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
    # 新代资源必须全新，不得复用旧代任何标识
    assert data["session_id"] != session["session_id"]
    assert data["room_id"] != session["room_id"]
    assert data["room_id"].startswith("wake-")
    assert data["user_sig"]
    assert data["expires_at"]
    assert data["scene"] == "trtc_full_duplex"
    # 账本侧：新会话 SIGNING、旧会话 wake 后置 SIGNING 防二次 wake
    from app.voice.control_plane import SessionLedger

    states = {row["session_id"]: row["state"]
              for row in SessionLedger(fx.store).list_sessions()}
    assert states[session["session_id"]] == "SIGNING"
    assert states[data["session_id"]] == "SIGNING"


def test_wake_replay_same_request_returns_original(fx) -> None:
    """重放同 payload（同 wake_event_id + 同客户端字段）返回原结果。"""
    session = _kws_ready(fx)
    body = _wake_body(session, str(uuid.uuid4()))
    first = fx.client.post("/api/v1/voice/sessions/wake", json=body,
                           headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    assert first.status_code == 201
    replay = fx.client.post("/api/v1/voice/sessions/wake", json=body,
                            headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    assert replay.status_code == 201
    assert replay.json()["data"] == first.json()["data"]
    # 重放必须返回持久化的原签发凭据，而非重放路径上重新签发的 user_sig
    assert replay.json()["data"]["user_sig"] == first.json()["data"]["user_sig"]


def test_wake_replay_different_payload_returns_40913(fx) -> None:
    """同 wake_event_id + 不同 payload（detected_at 变化）→ 40913。

    契约语义：wake 语境 message 为 wake_event_replayed_or_expired
    （区别于 termination 语境的 termination_not_retryable）。
    """
    session = _kws_ready(fx)
    wake_event_id = str(uuid.uuid4())
    first = fx.client.post(
        "/api/v1/voice/sessions/wake",
        json=_wake_body(session, wake_event_id),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    assert first.status_code == 201
    replay = fx.client.post(
        "/api/v1/voice/sessions/wake",
        json=_wake_body(session, wake_event_id, detected_at="2026-08-25T00:31:00Z"),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()))
    assert replay.status_code == 409
    body = replay.json()
    assert body["code"] == 40913
    assert body["message"] == "wake_event_replayed_or_expired"
