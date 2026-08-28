"""方案 B CP 会话账本行为测试。

测试通过真实 VoiceStore 和 SQLite 迁移验证持久化控制面语义。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.voice.control_plane import (
    IdempotencyConflict,
    InvalidTerminationState,
    SessionLedger,
)
from app.voice.storage import VoiceStore


ACK_NAMES = (
    "android_trtc_left",
    "sidecar_trtc_left",
    "bridge_drained_closed",
    "apm_cancelled_closed",
    "brain_turns_sealed",
)

# Authorized reporters per ack (P1-3, contract draft §4.1).
# bridge_drained_closed needs both sidecar + rtc_bridge; apm_cancelled_closed
# is reported by rtc_bridge (the process holding the APM session).
ACK_REPORTERS_BY_NAME = {
    "android_trtc_left": ("android",),
    "sidecar_trtc_left": ("sidecar",),
    "bridge_drained_closed": ("sidecar", "rtc_bridge"),
    "apm_cancelled_closed": ("rtc_bridge",),
    "brain_turns_sealed": ("brain",),
}


def _ledger_factory(tmp_path: Path) -> SessionLedger:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    return SessionLedger(store)


def _session(ledger: Any, *, generation: int = 7) -> dict[str, Any]:
    value = {
        "session_id": str(uuid.uuid4()),
        "device_id": str(uuid.uuid4()),
        "room_id": f"room-{uuid.uuid4()}",
        "generation": generation,
    }
    ledger.create_session(**value)
    return value


def _begin(ledger: Any, session: dict[str, Any], *, request_id: str | None = None,
           reason: str = "user_stop", payload_extra: dict[str, Any] | None = None) -> Any:
    payload = {
        "session_id": session["session_id"],
        "device_id": session["device_id"],
        "room_id": session["room_id"],
        "generation": session["generation"],
        "reason": reason,
    }
    payload.update(payload_extra or {})
    return ledger.begin_termination(
        session_id=session["session_id"],
        generation=session["generation"],
        request_id=request_id or str(uuid.uuid4()),
        payload=payload,
    )


def _report(ledger: Any, session: dict[str, Any], termination_id: str,
            acknowledgement: str, reporter: str, result: str = "confirmed",
            error_code: str | None = None) -> Any:
    return ledger.record_ack(
        termination_id=termination_id,
        session_id=session["session_id"],
        device_id=session["device_id"],
        room_id=session["room_id"],
        generation=session["generation"],
        acknowledgement=acknowledgement,
        reporter=reporter,
        result=result,
        error_code=error_code,
    )


def _report_all_sides(ledger: Any, session: dict[str, Any], termination_id: str,
                      acknowledgement: str, result: str = "confirmed") -> None:
    for reporter in ACK_REPORTERS_BY_NAME[acknowledgement]:
        _report(ledger, session, termination_id, acknowledgement, reporter, result)


def _ack_all(ledger: Any, session: dict[str, Any], termination: Any) -> None:
    termination_id = termination["termination_id"]
    for name in ACK_NAMES:
        _report_all_sides(ledger, session, termination_id, name, "confirmed")


def test_session_id_generation_persist_and_rebuild_are_readable(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger, generation=11)

    first = ledger.get_session(session["session_id"])
    assert first["session_id"] == session["session_id"]
    assert first["device_id"] == session["device_id"]
    assert first["room_id"] == session["room_id"]
    assert first["generation"] == 11

    rebuilt = _ledger_factory(tmp_path)
    restored = rebuilt.get_session(session["session_id"])
    assert restored == first


def test_terminate_same_request_same_payload_is_idempotent(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    request_id = str(uuid.uuid4())

    first = _begin(ledger, session, request_id=request_id)
    second = _begin(ledger, session, request_id=request_id)

    assert second["termination_id"] == first["termination_id"]
    assert second["state"] == "TERMINATING"
    assert ledger.count_terminations(session["session_id"]) == 1


def test_terminate_same_request_different_payload_is_conflict(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    request_id = str(uuid.uuid4())
    _begin(ledger, session, request_id=request_id, reason="user_stop")

    with pytest.raises(IdempotencyConflict, match="idempotency|payload"):
        _begin(ledger, session, request_id=request_id, reason="app_shutdown")


def test_ack_name_and_result_are_restricted(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)

    with pytest.raises(InvalidTerminationState, match="acknowledgement"):
        _report(ledger, session, termination["termination_id"], "unknown_ack", "android")
    with pytest.raises(InvalidTerminationState, match="result"):
        _report(ledger, session, termination["termination_id"],
                ACK_NAMES[0], "android", "invented")


def test_incomplete_acknowledgements_never_complete_or_authorize_kws(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)

    for name in ACK_NAMES[:-1]:
        _report_all_sides(ledger, session, termination["termination_id"], name, "confirmed")

    status = ledger.get_termination(termination["termination_id"])
    assert status["result"] == "pending"
    assert status["state"] == "TERMINATING"
    assert status["terminal_at"] is None
    assert ledger.can_enter_kws_ready(session["session_id"], session["generation"]) is False


def test_all_five_confirmed_acknowledgements_complete_and_enable_kws(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    _ack_all(ledger, session, termination)

    status = ledger.get_termination(termination["termination_id"])
    assert status["result"] == "complete"
    assert status["state"] == "TERMINATED"
    assert status["terminal_at"] is not None
    assert all(status["acknowledgements"][name] == "confirmed" for name in ACK_NAMES)
    assert ledger.mark_kws_ready(session["session_id"], session["generation"]) is True


@pytest.mark.parametrize("terminal_result", ["partial", "timeout"])
def test_partial_or_timeout_never_authorizes_kws(tmp_path: Path, terminal_result: str) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    ledger.finish_termination(termination["termination_id"], result=terminal_result)

    status = ledger.get_termination(termination["termination_id"])
    assert status["result"] == terminal_result
    assert status["state"] in {"TERMINATION_PARTIAL", "TERMINATION_TIMEOUT"}
    assert ledger.can_enter_kws_ready(session["session_id"], session["generation"]) is False


def test_retry_only_partial_or_timeout_creates_child_with_parent_and_new_request(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    parent = _begin(ledger, session)
    ledger.finish_termination(parent["termination_id"], result="partial")

    child = ledger.retry_termination(
        session_id=session["session_id"],
        parent_termination_id=parent["termination_id"],
        request_id=str(uuid.uuid4()),
        reason="retry_failed_acknowledgements",
    )

    assert child["termination_id"] != parent["termination_id"]
    assert child["parent_termination_id"] == parent["termination_id"]
    assert child["request_id"] != parent.get("request_id")
    assert child["state"] == "TERMINATING"

    with pytest.raises(InvalidTerminationState, match="not retryable"):
        ledger.retry_termination(
            session_id=session["session_id"],
            parent_termination_id=child["termination_id"],
            request_id=str(uuid.uuid4()),
            reason="retry_failed_acknowledgements",
        )


def test_retry_inherits_confirmed_acknowledgements_as_read_only(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    parent = _begin(ledger, session)
    for name in ACK_NAMES[:-1]:
        _report_all_sides(ledger, session, parent["termination_id"], name, "confirmed")
    ledger.finish_termination(parent["termination_id"], result="partial")

    child = ledger.retry_termination(
        session_id=session["session_id"],
        parent_termination_id=parent["termination_id"],
        request_id=str(uuid.uuid4()),
        reason="retry_failed_acknowledgements",
    )
    child_status = ledger.get_termination(child["termination_id"])
    assert all(child_status["acknowledgements"][name] == "confirmed" for name in ACK_NAMES[:-1])
    with pytest.raises(InvalidTerminationState):
        _report(ledger, session, child["termination_id"],
                ACK_NAMES[0], "android", "failed")


def test_concurrent_same_request_creates_one_termination(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    request_id = str(uuid.uuid4())

    async def issue() -> list[Any]:
        return await asyncio.gather(
            asyncio.to_thread(_begin, ledger, session, request_id=request_id),
            asyncio.to_thread(_begin, ledger, session, request_id=request_id),
        )

    first, second = asyncio.run(issue())
    assert first["termination_id"] == second["termination_id"]
    assert ledger.count_terminations(session["session_id"]) == 1


# ---- P0-1: begin_termination state-machine validation ----


def test_new_root_terminate_rejected_when_already_terminating(tmp_path: Path) -> None:
    """A second root terminate with a different request_id on a TERMINATING
    session must be rejected — only the same idempotent key is allowed."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    _begin(ledger, session, request_id="req-A")

    with pytest.raises(InvalidTerminationState, match="does not allow|conflict"):
        _begin(ledger, session, request_id="req-B")


def test_new_root_terminate_rejected_when_terminated(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    term = _begin(ledger, session)
    _ack_all(ledger, session, term)
    assert ledger.get_session(session["session_id"])["state"] == "TERMINATED"

    with pytest.raises(InvalidTerminationState, match="does not allow|conflict"):
        _begin(ledger, session, request_id="req-after-complete")


def test_new_root_terminate_rejected_when_kws_ready(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    term = _begin(ledger, session)
    _ack_all(ledger, session, term)
    assert ledger.mark_kws_ready(session["session_id"], session["generation"]) is True

    with pytest.raises(InvalidTerminationState, match="does not allow|conflict"):
        _begin(ledger, session, request_id="req-after-kws")


def test_same_idempotent_key_returns_existing_when_terminating(tmp_path: Path) -> None:
    """Re-issuing the same request_id+payload on a TERMINATING session returns
    the existing termination — this is valid idempotent replay."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    first = _begin(ledger, session, request_id="req-idempotent")
    second = _begin(ledger, session, request_id="req-idempotent")
    assert second["termination_id"] == first["termination_id"]


# ---- P0-2: retry_termination cycle closure ----


def test_retry_rejected_when_parent_has_open_child(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    parent = _begin(ledger, session)
    ledger.finish_termination(parent["termination_id"], result="partial")
    child = ledger.retry_termination(
        session_id=session["session_id"],
        parent_termination_id=parent["termination_id"],
        request_id="retry-1",
        reason="retry_failed_acknowledgements",
    )
    # child is still pending → cannot create another retry from the same parent
    with pytest.raises(InvalidTerminationState, match="in-progress|not retryable|does not allow retry"):
        ledger.retry_termination(
            session_id=session["session_id"],
            parent_termination_id=parent["termination_id"],
            request_id="retry-2",
            reason="retry_failed_acknowledgements",
        )
    assert ledger.count_terminations(session["session_id"]) == 2


def test_retry_rejected_after_child_complete(tmp_path: Path) -> None:
    """Once a child retry has completed, the parent cycle is closed and cannot
    be retried again."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    parent = _begin(ledger, session)
    # ack 4 of 5, then finish as partial
    for name in ACK_NAMES[:-1]:
        _report_all_sides(ledger, session, parent["termination_id"], name, "confirmed")
    ledger.finish_termination(parent["termination_id"], result="partial")
    # retry → child
    child = ledger.retry_termination(
        session_id=session["session_id"],
        parent_termination_id=parent["termination_id"],
        request_id="retry-ok",
        reason="retry_failed_acknowledgements",
    )
    # complete the child (last ack + all inherited = 5 confirmed)
    _report_all_sides(ledger, session, child["termination_id"],
                      ACK_NAMES[-1], "confirmed")
    assert ledger.get_termination(child["termination_id"])["result"] == "complete"
    # attempt to retry the same parent again → rejected
    with pytest.raises(InvalidTerminationState, match="closed|not retryable|does not allow retry"):
        ledger.retry_termination(
            session_id=session["session_id"],
            parent_termination_id=parent["termination_id"],
            request_id="retry-3",
            reason="retry_failed_acknowledgements",
        )


def test_retry_rejected_when_session_not_in_recovery_state(tmp_path: Path) -> None:
    """Retry is only allowed when the session is in TERMINATION_PARTIAL or
    TERMINATION_TIMEOUT — not when it is ACTIVE or TERMINATING."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    parent = _begin(ledger, session)
    # session is TERMINATING, not a recovery state → reject
    with pytest.raises(InvalidTerminationState, match="does not allow retry|not retryable"):
        ledger.retry_termination(
            session_id=session["session_id"],
            parent_termination_id=parent["termination_id"],
            request_id="retry-bad-state",
            reason="retry_failed_acknowledgements",
        )


# ---- P1-2: request_id unique across operations, reason validation ----


def test_request_id_cannot_be_reused_across_terminate_and_retry(tmp_path: Path) -> None:
    """request_id is unique per (session, generation) regardless of operation."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    shared_request = "shared-req-id"
    parent = _begin(ledger, session, request_id=shared_request)
    ledger.finish_termination(parent["termination_id"], result="partial")

    with pytest.raises((IdempotencyConflict, InvalidTerminationState)):
        ledger.retry_termination(
            session_id=session["session_id"],
            parent_termination_id=parent["termination_id"],
            request_id=shared_request,  # same as parent's
            reason="retry_failed_acknowledgements",
        )


def test_retry_reason_must_match_parent_result(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    parent = _begin(ledger, session)
    ledger.finish_termination(parent["termination_id"], result="timeout")

    # timeout parent requires reason='retry_timeout', not 'retry_failed_acknowledgements'
    with pytest.raises(InvalidTerminationState, match="reason|not retryable"):
        ledger.retry_termination(
            session_id=session["session_id"],
            parent_termination_id=parent["termination_id"],
            request_id=str(uuid.uuid4()),
            reason="retry_failed_acknowledgements",
        )


# ---- P1-4: get_termination always returns all 5 ack names ----


def test_get_termination_returns_pending_for_missing_acks(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    # record only one ack
    _report_all_sides(ledger, session, termination["termination_id"],
                      ACK_NAMES[0], "confirmed")
    status = ledger.get_termination(termination["termination_id"])
    assert status["acknowledgements"][ACK_NAMES[0]] == "confirmed"
    for name in ACK_NAMES[1:]:
        assert status["acknowledgements"][name] == "pending", f"{name} should be pending"


# ---- P2-3: concurrent different requests ----


def test_concurrent_different_requests_create_separate_terminations(tmp_path: Path) -> None:
    """Two different request_ids on an ACTIVE session: the first wins (session
    goes to TERMINATING), the second is rejected."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)

    results: list[Any] = []

    async def race() -> None:
        def _do(req: str) -> Any:
            try:
                return _begin(ledger, session, request_id=req)
            except Exception as exc:
                return exc
        a, b = await asyncio.gather(
            asyncio.to_thread(_do, "req-X"),
            asyncio.to_thread(_do, "req-Y"),
        )
        results.extend([a, b])

    asyncio.run(race())
    # one should succeed, the other should raise
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InvalidTerminationState)


# ---- P1-3: reporter-bound ack reports with bridge dual-report aggregation ----


def test_unauthorized_reporter_is_rejected(tmp_path: Path) -> None:
    """Each ack only accepts reports from its authorized reporter — a reporter
    cannot impersonate another component's acknowledgement."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)

    # android cannot report sidecar_trtc_left
    with pytest.raises(InvalidTerminationState, match="not authorized"):
        _report(ledger, session, termination["termination_id"],
                "sidecar_trtc_left", "android")
    # brain cannot report android_trtc_left
    with pytest.raises(InvalidTerminationState, match="not authorized"):
        _report(ledger, session, termination["termination_id"],
                "android_trtc_left", "brain")
    # unknown reporter
    with pytest.raises(InvalidTerminationState, match="not authorized"):
        _report(ledger, session, termination["termination_id"],
                "android_trtc_left", "attacker")
    # nothing was recorded
    status = ledger.get_termination(termination["termination_id"])
    assert all(v == "pending" for v in status["acknowledgements"].values())


def test_bridge_ack_requires_both_sidecar_and_rtc_bridge_reports(tmp_path: Path) -> None:
    """bridge_drained_closed stays pending with only ONE side reported; both
    sidecar AND rtc_bridge must confirm before it is confirmed."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    tid = termination["termination_id"]

    # confirm all other 4 acks fully
    for name in ACK_NAMES:
        if name != "bridge_drained_closed":
            _report_all_sides(ledger, session, tid, name, "confirmed")

    # only sidecar reports bridge → still pending
    _report(ledger, session, tid, "bridge_drained_closed", "sidecar")
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["bridge_drained_closed"] == "pending"
    assert status["result"] == "pending"  # not complete

    # rtc_bridge also reports → now confirmed → termination completes
    _report(ledger, session, tid, "bridge_drained_closed", "rtc_bridge")
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["bridge_drained_closed"] == "confirmed"
    assert status["result"] == "complete"
    assert status["state"] == "TERMINATED"


def test_bridge_ack_accepts_reports_in_any_order(tmp_path: Path) -> None:
    """Out-of-order dual reports still aggregate correctly."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    tid = termination["termination_id"]

    # rtc_bridge first, sidecar second
    _report(ledger, session, tid, "bridge_drained_closed", "rtc_bridge")
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["bridge_drained_closed"] == "pending"
    _report(ledger, session, tid, "bridge_drained_closed", "sidecar")
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["bridge_drained_closed"] == "confirmed"


def test_duplicate_report_latest_wins(tmp_path: Path) -> None:
    """A reporter re-reporting overwrites its previous report (latest wins)."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    tid = termination["termination_id"]

    # sidecar confirms, then re-reports failed → aggregate becomes failed
    _report(ledger, session, tid, "bridge_drained_closed", "sidecar", "confirmed")
    _report(ledger, session, tid, "bridge_drained_closed", "sidecar", "failed",
            error_code="BRIDGE_DRAIN_FAILED")
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["bridge_drained_closed"] == "failed"

    # sidecar re-reports confirmed → aggregate back to pending (rtc_bridge still missing)
    _report(ledger, session, tid, "bridge_drained_closed", "sidecar", "confirmed")
    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["bridge_drained_closed"] == "pending"


def test_failed_report_blocks_completion(tmp_path: Path) -> None:
    """A failed ack blocks termination completion even if all others confirm."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    tid = termination["termination_id"]

    for name in ACK_NAMES:
        if name != "apm_cancelled_closed":
            _report_all_sides(ledger, session, tid, name, "confirmed")
    _report(ledger, session, tid, "apm_cancelled_closed", "rtc_bridge", "failed",
            error_code="APM_CLOSE_TIMEOUT")

    status = ledger.get_termination(tid)
    assert status["acknowledgements"]["apm_cancelled_closed"] == "failed"
    assert status["result"] == "pending"  # cannot complete with a failed ack
    assert ledger.can_enter_kws_ready(session["session_id"], session["generation"]) is False


# ---- P1-5: KWS readiness evidence ----


def test_mark_kws_ready_records_persisted_evidence(tmp_path: Path) -> None:
    """mark_kws_ready persists reporter-bound readiness evidence in the same
    transaction as the state transition."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    _ack_all(ledger, session, termination)

    evidence = {
        "kws_initialized": True,
        "audio_focus_released": True,
        "trtc_capture_stopped": True,
    }
    ok = ledger.mark_kws_ready(
        session["session_id"], session["generation"],
        reporter="android", evidence=evidence,
    )
    assert ok is True
    assert ledger.get_session(session["session_id"])["state"] == "KWS_READY"

    readiness = ledger.get_kws_readiness(session["session_id"], session["generation"])
    assert len(readiness) == 1
    assert readiness[0]["reporter"] == "android"
    assert readiness[0]["evidence"] == evidence
    assert readiness[0]["recorded_at"] > 0


def test_mark_kws_ready_rejects_non_scalar_evidence(tmp_path: Path) -> None:
    """Evidence values must be scalars — no nested objects or secrets."""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    _ack_all(ledger, session, termination)

    with pytest.raises(InvalidTerminationState, match="scalar"):
        ledger.mark_kws_ready(
            session["session_id"], session["generation"],
            reporter="android",
            evidence={"kws_initialized": {"nested": "object"}},
        )
    # session state unchanged
    assert ledger.get_session(session["session_id"])["state"] == "TERMINATED"


def test_mark_kws_ready_without_evidence_still_records_reporter(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)
    termination = _begin(ledger, session)
    _ack_all(ledger, session, termination)

    assert ledger.mark_kws_ready(session["session_id"], session["generation"]) is True
    readiness = ledger.get_kws_readiness(session["session_id"], session["generation"])
    assert len(readiness) == 1
    assert readiness[0]["reporter"] == "android"
    assert readiness[0]["evidence"] == {}



# ---- wake: consume_wake (KWS_READY -> SIGNING(n+1)) ----


def _kws_ready_session(ledger: Any, *, generation: int = 7) -> dict[str, Any]:
    """Create a session, complete termination, mark KWS ready."""
    session = _session(ledger, generation=generation)
    termination = _begin(ledger, session)
    _ack_all(ledger, session, termination)
    assert ledger.mark_kws_ready(session["session_id"], generation) is True
    return session


def _wake_kwargs(session: dict[str, Any], wake_event_id: str,
                 *, user_sig: str = "sig-new-1",
                 expires_at: float = 4102444800.0) -> dict[str, Any]:
    return dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        prior_generation=session["generation"],
        wake_event_id=wake_event_id,
        user_sig=user_sig,
        expires_at=expires_at,
    )


def test_consume_wake_moves_kws_ready_session_to_signing_next_generation(
    tmp_path: Path,
) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger, generation=7)
    wake_event_id = str(uuid.uuid4())

    record = ledger.consume_wake(
        session_id=str(uuid.uuid4()),
        **_wake_kwargs(session, wake_event_id),
    )

    assert record["prior_generation"] == 7
    assert record["generation"] == 8
    assert record["wake_event_id"] == wake_event_id
    assert record["session_id"] != session["session_id"]
    assert record["room_id"] != session["room_id"]
    assert record["state"] == "SIGNING"
    new_session = ledger.get_session(record["session_id"])
    assert new_session["generation"] == 8
    assert new_session["state"] == "SIGNING"


def test_consume_wake_requires_kws_ready_prior_session(tmp_path: Path) -> None:
    """旧会话不是 KWS_READY（如仍 ACTIVE）→ 拒绝。"""
    ledger = _ledger_factory(tmp_path)
    session = _session(ledger)  # ACTIVE, no termination

    with pytest.raises(InvalidTerminationState) as exc_info:
        ledger.consume_wake(session_id=str(uuid.uuid4()),
                            **_wake_kwargs(session, str(uuid.uuid4())))
    assert exc_info.value.code == 40917


def test_consume_wake_rejects_generation_mismatch(tmp_path: Path) -> None:
    """prior_generation 与账本不符 → stale generation 40917。"""
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger, generation=7)
    wrong = dict(session, generation=6)

    with pytest.raises(InvalidTerminationState) as exc_info:
        ledger.consume_wake(session_id=str(uuid.uuid4()),
                            **_wake_kwargs(wrong, str(uuid.uuid4())))
    assert exc_info.value.code == 40917


def test_consume_wake_rejects_wrong_device(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    kwargs = _wake_kwargs(session, str(uuid.uuid4()))
    kwargs["device_id"] = str(uuid.uuid4())

    with pytest.raises(InvalidTerminationState) as exc_info:
        ledger.consume_wake(session_id=str(uuid.uuid4()), **kwargs)
    assert exc_info.value.code == 40917


def test_consume_wake_replay_returns_original_record(tmp_path: Path) -> None:
    """同一 wake_event_id 重放返回原结果，不创建第二个会话。"""
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    wake_event_id = str(uuid.uuid4())
    new_session_id = str(uuid.uuid4())

    first = ledger.consume_wake(session_id=new_session_id,
                                **_wake_kwargs(session, wake_event_id))
    replay = ledger.consume_wake(session_id=new_session_id,
                                 **_wake_kwargs(session, wake_event_id))

    assert replay == first
    sessions = [row["session_id"] for row in ledger.list_sessions()]
    assert sessions.count(first["session_id"]) == 1


def test_consume_wake_replay_with_different_payload_rejected(tmp_path: Path) -> None:
    """同 wake_event_id 不同 payload（如不同 room）→ 40913。"""
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    wake_event_id = str(uuid.uuid4())

    first = ledger.consume_wake(
        session_id=str(uuid.uuid4()),
        detected_at="2026-08-25T00:30:00Z",
        **_wake_kwargs(session, wake_event_id))
    with pytest.raises(InvalidTerminationState) as exc_info:
        ledger.consume_wake(
            session_id=str(uuid.uuid4()),
            detected_at="2026-08-25T00:31:00Z",  # different client payload
            **_wake_kwargs(session, wake_event_id))
    assert exc_info.value.code == 40913


def test_consume_wake_rejects_when_device_has_active_session(tmp_path: Path) -> None:
    """当前存在活动/终止中会话 → 拒绝（不得并发两会话）。"""
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger, generation=7)
    # device 另有一个 ACTIVE 会话
    other = _session(ledger, generation=8)
    ledger.create_session(**dict(other, device_id=session["device_id"],
                                 session_id=str(uuid.uuid4())))

    with pytest.raises(InvalidTerminationState) as exc_info:
        ledger.consume_wake(session_id=str(uuid.uuid4()),
                            **_wake_kwargs(session, str(uuid.uuid4())))
    assert exc_info.value.code == 40917


def test_consume_wake_marks_prior_session_signing_to_block_double_wake(
    tmp_path: Path,
) -> None:
    """首次 wake 后旧会话进入 SIGNING，二次 wake（新 event id）不得再从它签发。"""
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    ledger.consume_wake(session_id=str(uuid.uuid4()),
                        **_wake_kwargs(session, str(uuid.uuid4())))

    with pytest.raises(InvalidTerminationState) as exc_info:
        ledger.consume_wake(session_id=str(uuid.uuid4()),
                            **_wake_kwargs(session, str(uuid.uuid4())))
    assert exc_info.value.code == 40917


def test_consume_wake_persists_across_rebuild(tmp_path: Path) -> None:
    """wake 记录持久化：重建 ledger 后重放仍返回原记录。"""
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    wake_event_id = str(uuid.uuid4())
    new_session_id = str(uuid.uuid4())
    first = ledger.consume_wake(session_id=new_session_id,
                                **_wake_kwargs(session, wake_event_id))

    rebuilt = _ledger_factory(tmp_path)
    replay = rebuilt.consume_wake(session_id=new_session_id,
                                  **_wake_kwargs(session, wake_event_id))
    assert replay == first
