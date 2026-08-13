"""Secured sidecar pending control-plane contract.

The endpoint is a one-shot control-plane claim. It carries only session metadata,
never PCM/audio payloads, and is protected by sidecar bearer, nonce, and limits.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import create_secured_voice_router
from app.voice.auth import CredentialValidator
from app.voice.config import VoiceSecurityConfig, build_sidecar_credential_hashes
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore

DEVICE = "dev-a-000000000000000000000001"
DEVICE_SECRET = "secret-a-0123456789abcdef01234567"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
SIDECAR_NEXT_SECRET = "sidecar-next-secret-0123456789ab"
SDK_APP_ID = 1600155678
TRTC_SECRET = "fake-secret-key-for-test-only-0123456789"


def _nonce() -> str:
    return uuid.uuid4().hex


def _fixture(
    tmp_path: Path,
    device_limit: int = 100,
    *,
    rotating: bool = False,
) -> tuple[VoiceStore, TestClient]:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE, DEVICE_SECRET, device_name="phone-a")
    now = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)
    sidecar_credentials = build_sidecar_credential_hashes(
        current_secret=SIDECAR_SECRET,
        next_secret=SIDECAR_NEXT_SECRET if rotating else "",
        next_enabled_at="2026-08-09T01:00:00Z" if rotating else "",
        next_expires_at="2026-08-09T01:10:00Z" if rotating else "",
        config_revision="pending-r2" if rotating else "",
    )
    security = VoiceSecurityConfig(
        production=False,
        tls_enabled=True,
        owner_credential_hash=CredentialValidator.hash_credential("owner-secret"),
        sidecar_credential_hash=sidecar_credentials.current_hash,
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=SDK_APP_ID,
        trtc_secret_key=TRTC_SECRET,
    )
    validator = CredentialValidator(
        store, security.owner_credential_hash, sidecar_credentials, clock=lambda: now
    )
    service = RtcSessionService(
        RtcSessionConfig(sdk_app_id=SDK_APP_ID, secret_key=TRTC_SECRET, room_prefix="jax-")
    )
    app = FastAPI()
    app.include_router(
        create_secured_voice_router(
            store=store,
            service=service,
            validator=validator,
            nonces=NonceService(store),
            limiter=RateLimiter(
                store,
                RateLimitConfig(
                    window_seconds=60, device_limit=device_limit, ip_limit=100,
                ),
            ),
            security=security,
        )
    )
    return store, TestClient(app)


def _device_headers(nonce: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {DEVICE}.{DEVICE_SECRET}"}
    if nonce is not None:
        headers["X-Request-Nonce"] = nonce
    return headers


def _sidecar_headers(
    nonce: str | None = None,
    secret: str = SIDECAR_SECRET,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if nonce is not None:
        headers["X-Request-Nonce"] = nonce
    return headers


def _create_pending(client: TestClient) -> None:
    response = client.post(
        "/api/v1/voice/session",
        json={"device_id": DEVICE, "entry_point": "main"},
        headers=_device_headers(_nonce()),
    )
    assert response.status_code == 201


def test_pending_requires_sidecar_bearer_and_nonce(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path)
    no_auth = client.get("/api/v1/voice/session/pending")
    assert no_auth.status_code == 401
    assert no_auth.json()["code"] == 40101
    missing_nonce = client.get(
        "/api/v1/voice/session/pending", headers=_sidecar_headers()
    )
    assert missing_nonce.status_code == 401
    assert missing_nonce.json()["code"] == 40102


def test_pending_claim_is_one_shot_and_contains_no_audio(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path)
    _create_pending(client)
    first = client.get(
        "/api/v1/voice/session/pending", headers=_sidecar_headers(_nonce())
    )
    assert first.status_code == 200
    intents = first.json()["data"]["intents"]
    assert len(intents) == 1
    assert {"device_id", "room_id", "session_id"}.issubset(intents[0])
    assert not any("audio" in key or "pcm" in key for key in intents[0])
    second = client.get(
        "/api/v1/voice/session/pending", headers=_sidecar_headers(_nonce())
    )
    assert second.status_code == 200
    assert second.json()["data"]["intents"] == []


def test_pending_claim_is_atomic_under_concurrency(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path)
    _create_pending(client)
    statuses: list[int] = []
    payloads: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def request() -> None:
        barrier.wait()
        response = client.get(
            "/api/v1/voice/session/pending", headers=_sidecar_headers(_nonce())
        )
        with lock:
            statuses.append(response.status_code)
            payloads.append(response.json())

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses == [200] * 4
    assert sum(bool(payload["data"]["intents"]) for payload in payloads) == 1


def test_pending_rate_limit_is_enforced(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path, device_limit=1)
    headers = _sidecar_headers(_nonce())
    first = client.get("/api/v1/voice/session/pending", headers=headers)
    assert first.status_code == 200
    limited = client.get(
        "/api/v1/voice/session/pending", headers=_sidecar_headers(_nonce())
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == 42901


def test_current_and_next_share_pending_nonce_and_rate_limit_bucket(tmp_path: Path) -> None:
    _store, client = _fixture(tmp_path, device_limit=1, rotating=True)
    nonce = _nonce()
    current = client.get(
        "/api/v1/voice/session/pending", headers=_sidecar_headers(nonce, SIDECAR_SECRET)
    )
    assert current.status_code == 200
    replay_with_next = client.get(
        "/api/v1/voice/session/pending",
        headers=_sidecar_headers(nonce, SIDECAR_NEXT_SECRET),
    )
    assert replay_with_next.status_code == 401
    assert replay_with_next.json()["code"] == 40102
    limited_with_next = client.get(
        "/api/v1/voice/session/pending",
        headers=_sidecar_headers(_nonce(), SIDECAR_NEXT_SECRET),
    )
    assert limited_with_next.status_code == 429
    assert limited_with_next.json()["code"] == 42901
