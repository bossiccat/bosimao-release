"""Regression tests for commercial voice credential, claim and revocation hardening."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import create_secured_voice_router
from app.voice.auth import CredentialValidator
from app.voice.config import VoiceSecurityConfig, build_sidecar_credential_hashes
from app.voice.devices import DeviceService, RevokeTerminationError
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore

DEVICE_ID = "dev-a-000000000000000000000001"
DEVICE_SECRET = "secret-a-0123456789abcdef01234567"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
SDK_APP_ID = 1600155678
TRTC_SECRET = "fake-secret-key-for-test-only-0123456789"


def _nonce() -> str:
    return uuid.uuid4().hex


class RecordingTerminator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def terminate_and_wait(self, device_id: str, session_ids: list[str]) -> list[str]:
        self.calls.append((device_id, tuple(session_ids)))
        if self.fail:
            raise TimeoutError("termination not confirmed")
        return list(session_ids)


def _fixture(tmp_path: Path) -> tuple[VoiceStore, TestClient]:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE_ID, DEVICE_SECRET)
    credentials = build_sidecar_credential_hashes(current_secret=SIDECAR_SECRET)
    security = VoiceSecurityConfig(
        production=False,
        tls_enabled=True,
        owner_credential_hash=CredentialValidator.hash_credential("owner-secret"),
        sidecar_credential_hash=credentials.current_hash,
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=SDK_APP_ID,
        trtc_secret_key=TRTC_SECRET,
        rtc_termination_enabled=True,
    )
    app = FastAPI()
    app.include_router(create_secured_voice_router(
        store=store,
        service=RtcSessionService(RtcSessionConfig(
            sdk_app_id=SDK_APP_ID, secret_key=TRTC_SECRET, room_prefix="jax-"
        )),
        validator=CredentialValidator(store, security.owner_credential_hash, credentials),
        nonces=NonceService(store),
        limiter=RateLimiter(store, RateLimitConfig(device_limit=1000, ip_limit=1000)),
        security=security,
    ))
    return store, TestClient(app)


def _device_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DEVICE_ID}.{DEVICE_SECRET}",
        "X-Request-Nonce": _nonce(),
    }


def _sidecar_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SIDECAR_SECRET}",
        "X-Request-Nonce": _nonce(),
    }


def test_legacy_device_rows_receive_deterministic_persistent_credential_id(tmp_path: Path) -> None:
    db_path = tmp_path / "voice.db"
    sql = (Path(__file__).parents[2] / "app" / "voice" / "migrations" /
           "001_commercial_voice.sql").read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO device_credentials"
            " (device_id, device_name, platform, credential_hash, status, expires_at, created_at, updated_at)"
            " VALUES ('legacy-device', 'phone', 'android', 'salt$hash', 'active', 9999999999, 1, 1)"
        )
    first = VoiceStore(db_path)
    first.initialize()
    with first.connect() as conn:
        row = conn.execute(
            "SELECT credential_id FROM device_credentials WHERE device_id='legacy-device'"
        ).fetchone()
        columns = {item[1]: item for item in conn.execute("PRAGMA table_info(device_credentials)")}
    assert row is not None and row[0]
    assert columns["credential_id"][3] == 1
    first_id = row[0]
    first.initialize()
    with first.connect() as conn:
        second_id = conn.execute(
            "SELECT credential_id FROM device_credentials WHERE device_id='legacy-device'"
        ).fetchone()[0]
    assert second_id == first_id


def test_device_validator_returns_registered_persistent_credential_id(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    credential_id = str(uuid.uuid4())
    store.save_device(DEVICE_ID, DEVICE_SECRET, credential_id=credential_id)
    principal = CredentialValidator(store).verify_device(f"{DEVICE_ID}.{DEVICE_SECRET}")
    assert principal.credential_id == credential_id


def test_pending_claim_token_is_required_bound_and_single_use(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path)
    issued = client.post(
        "/api/v1/voice/session",
        json={"device_id": DEVICE_ID, "entry_point": "main"},
        headers=_device_headers(),
    )
    assert issued.status_code == 201
    pending = client.get("/api/v1/voice/session/pending", headers=_sidecar_headers())
    assert pending.status_code == 200
    intent = pending.json()["data"]["intents"][0]
    assert intent["claim_token"]

    tampered = client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": intent["session_id"],
            "claim_token": "x" * 32,
            "device_id": intent["device_id"],
            "user_id": "jax-pc-sidecar",
        },
        headers=_sidecar_headers(),
    )
    assert tampered.status_code == 409
    assert tampered.json()["code"] == 40901

    ok = client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": intent["session_id"],
            "claim_token": intent["claim_token"],
            "device_id": intent["device_id"],
            "user_id": "jax-pc-sidecar",
        },
        headers=_sidecar_headers(),
    )
    assert ok.status_code == 201
    assert ok.json()["data"]["room_id"] == intent["room_id"]

    replay = client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": intent["session_id"],
            "claim_token": intent["claim_token"],
            "device_id": intent["device_id"],
            "user_id": "jax-pc-sidecar",
        },
        headers=_sidecar_headers(),
    )
    assert replay.status_code == 409
    assert replay.json()["code"] == 40901


def test_sidecar_sign_rejects_without_claim_and_rejects_arbitrary_user(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path)
    no_claim = client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": str(uuid.uuid4()),
            "claim_token": "x" * 32,
            "device_id": DEVICE_ID,
            "user_id": "jax-pc-sidecar",
        },
        headers=_sidecar_headers(),
    )
    assert no_claim.status_code == 409
    arbitrary = client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": str(uuid.uuid4()),
            "claim_token": "x" * 32,
            "device_id": DEVICE_ID,
            "user_id": "sidecar-admin",
        },
        headers=_sidecar_headers(),
    )
    assert arbitrary.status_code == 422


def test_claim_rejected_after_target_device_revoked(tmp_path: Path) -> None:
    store, client = _fixture(tmp_path)
    issued = client.post(
        "/api/v1/voice/session",
        json={"device_id": DEVICE_ID, "entry_point": "main"},
        headers=_device_headers(),
    )
    assert issued.status_code == 201
    intent = client.get(
        "/api/v1/voice/session/pending", headers=_sidecar_headers()
    ).json()["data"]["intents"][0]
    store.revoke_device(DEVICE_ID, "lost")
    signed = client.post(
        "/api/v1/voice/session/sign",
        json={
            "session_id": intent["session_id"],
            "claim_token": intent["claim_token"],
            "device_id": DEVICE_ID,
            "user_id": "jax-pc-sidecar",
        },
        headers=_sidecar_headers(),
    )
    assert signed.status_code == 409
    assert signed.json()["code"] == 40901


def test_revoke_requires_confirmed_termination_and_never_defaults_to_success(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE_ID, DEVICE_SECRET)
    user_sig = "signed-value"
    DeviceService(store, terminator=RecordingTerminator()).record_session_issued(
        "session-1", DEVICE_ID, user_sig, time.time() + 600
    )
    missing = DeviceService(store)
    with pytest.raises(RevokeTerminationError):
        missing.revoke_device(DEVICE_ID, "lost")


def test_revoke_calls_terminator_and_records_only_confirmed_sessions(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE_ID, DEVICE_SECRET)
    terminator = RecordingTerminator()
    devices = DeviceService(store, terminator=terminator)
    devices.record_session_issued("session-1", DEVICE_ID, "signed-value", time.time() + 600)
    result = devices.revoke_device(DEVICE_ID, "lost")
    assert terminator.calls == [(DEVICE_ID, ("session-1",))]
    assert result["terminated_session_ids"] == ["session-1"]
    with store.connect() as conn:
        event = conn.execute(
            "SELECT event_type FROM session_events WHERE session_id='session-1'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event[0] == "terminated"
