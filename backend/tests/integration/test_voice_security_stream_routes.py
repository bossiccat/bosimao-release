"""Secured voice status, stream, runtime gate, and leakage integration tests."""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest

from .voice_security_fixture import (
    DEVICE_A,
    DEVICE_B,
    FAKE_SECRET_KEY,
    SECRET_A,
    SECRET_B,
    SIDECAR_SECRET,
    VoiceSecurityFixture,
    nonce,
)


@pytest.fixture
def fx(tmp_path: Path) -> VoiceSecurityFixture:
    return VoiceSecurityFixture(tmp_path)


def test_status_is_scoped_by_principal(fx: VoiceSecurityFixture) -> None:
    session_a = uuid.uuid4().hex
    session_b = uuid.uuid4().hex
    fx.store.write_session_event(session_id=session_a, device_id=DEVICE_A, event_type="issued")
    fx.store.write_session_event(session_id=session_b, device_id=DEVICE_B, event_type="issued")
    resp = fx.client.get(
        "/api/v1/voice/status",
        headers=fx.auth_headers(DEVICE_A, SECRET_A),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert session_a in json.dumps(data)
    assert session_b not in json.dumps(data)


def test_status_without_bearer_rejected(fx: VoiceSecurityFixture) -> None:
    resp = fx.client.get("/api/v1/voice/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_stream_upgrade_requires_credential(fx: VoiceSecurityFixture) -> None:
    with pytest.raises(Exception):
        with fx.client.websocket_connect("/api/v1/voice/stream"):
            pass


def test_stream_upgrade_requires_nonce(fx: VoiceSecurityFixture) -> None:
    headers = {"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}"}
    with pytest.raises(Exception):
        with fx.client.websocket_connect("/api/v1/voice/stream", headers=headers):
            pass


def test_existing_stream_is_closed_after_credential_revocation(fx: VoiceSecurityFixture) -> None:
    headers = {"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}", "X-Request-Nonce": nonce()}
    with fx.client.websocket_connect("/api/v1/voice/stream", headers=headers) as ws:
        ws.send_json({
            "type": "hello",
            "session_id": "session-active",
            "device_id": DEVICE_A,
            "protocol_version": "1.0",
            "audio_format": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 16000,
                "channels": 1,
                "frame_ms": 20,
                "frame_bytes": 640,
            },
        })
        assert ws.receive_json()["type"] == "ready"
        fx.store.revoke_device(DEVICE_A, reason="lost")
        ws.send_json({"type": "heartbeat"})
        with pytest.raises(Exception):
            ws.receive_json()


def test_stream_revoked_credential_rejected(fx: VoiceSecurityFixture) -> None:
    fx.store.revoke_device(DEVICE_B, reason="lost")
    headers = {"Authorization": f"Bearer {DEVICE_B}.{SECRET_B}", "X-Request-Nonce": nonce()}
    with pytest.raises(Exception):
        with fx.client.websocket_connect("/api/v1/voice/stream", headers=headers):
            pass


def test_stream_hello_must_bind_to_principal(fx: VoiceSecurityFixture) -> None:
    headers = {"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}", "X-Request-Nonce": nonce()}
    with fx.client.websocket_connect("/api/v1/voice/stream", headers=headers) as ws:
        ws.send_json({
            "type": "hello",
            "session_id": uuid.uuid4().hex,
            "device_id": DEVICE_B,
            "protocol_version": "1.0",
            "audio_format": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 16000,
                "channels": 1,
                "frame_ms": 20,
                "frame_bytes": 640,
            },
        })
        with pytest.raises(Exception):
            ws.receive_json()


def test_stream_hello_ok_for_bound_principal(fx: VoiceSecurityFixture) -> None:
    headers = {"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}", "X-Request-Nonce": nonce()}
    with fx.client.websocket_connect("/api/v1/voice/stream", headers=headers) as ws:
        ws.send_json({
            "type": "hello",
            "session_id": uuid.uuid4().hex,
            "device_id": DEVICE_A,
            "protocol_version": "1.0",
            "audio_format": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 16000,
                "channels": 1,
                "frame_ms": 20,
                "frame_bytes": 640,
            },
        })
        msg = ws.receive_json()
        assert msg["type"] == "ready"


def test_runtime_gate_returns_50300_when_required_items_missing(tmp_path: Path) -> None:
    fx = VoiceSecurityFixture(tmp_path)
    fx.security.trtc_secret_key = ""
    resp = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_A),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce()),
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == 50300
    sign = fx.client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": "session-gated",
            "claim_token": "x" * 32,
            "device_id": DEVICE_A,
            "user_id": "jax-pc-sidecar",
        },
        headers={"Authorization": f"Bearer {SIDECAR_SECRET}", "X-Request-Nonce": nonce()},
    )
    assert sign.status_code == 503
    assert sign.json()["code"] == 50300
    status = fx.client.get(
        "/api/v1/voice/status", headers=fx.auth_headers(DEVICE_A, SECRET_A)
    )
    assert status.status_code == 503
    assert status.json()["code"] == 50300


def test_errors_and_logs_do_not_leak_secrets(fx: VoiceSecurityFixture, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        bad = fx.client.post(
            "/api/v1/voice/session",
            json=fx.session_payload(DEVICE_A),
            headers=fx.auth_headers(DEVICE_A, "wrong-secret-value-0123456789", nonce()),
        )
        assert bad.status_code == 401
        assert SECRET_A not in bad.text
        assert "wrong-secret-value-0123456789" not in bad.text
    assert "wrong-secret-value-0123456789" not in caplog.text
    assert SECRET_A not in caplog.text
    assert SIDECAR_SECRET not in caplog.text
    assert FAKE_SECRET_KEY not in caplog.text
