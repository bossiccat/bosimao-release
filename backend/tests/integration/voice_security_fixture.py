"""Shared real FastAPI/SQLite fixture for secured voice route tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
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

DEVICE_A = "dev-a-000000000000000000000001"
DEVICE_B = "dev-b-000000000000000000000002"
SECRET_A = "secret-a-0123456789abcdef01234567"
SECRET_B = "secret-b-0123456789abcdef01234567"
OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
SIDECAR_NEXT_SECRET = "sidecar-next-secret-0123456789ab"
FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"


class VoiceSecurityFixture:
    def __init__(
        self,
        tmp_path: Path,
        device_limit: int = 1000,
        ip_limit: int = 1000,
        *,
        rotating: bool = False,
    ):
        self.store = VoiceStore(tmp_path / "voice.db")
        self.store.initialize()
        self.store.save_device(DEVICE_A, SECRET_A, device_name="phone-a")
        self.store.save_device(DEVICE_B, SECRET_B, device_name="phone-b")
        now = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)
        sidecar_credentials = build_sidecar_credential_hashes(
            current_secret=SIDECAR_SECRET,
            next_secret=SIDECAR_NEXT_SECRET if rotating else "",
            next_enabled_at="2026-08-09T01:00:00Z" if rotating else "",
            next_expires_at="2026-08-09T01:10:00Z" if rotating else "",
            config_revision="sign-r2" if rotating else "",
        )
        security = VoiceSecurityConfig(
            production=False,
            tls_enabled=True,
            owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
            sidecar_credential_hash=sidecar_credentials.current_hash,
            nonce_enabled=True,
            rate_limit_enabled=True,
            trtc_sdk_app_id=FAKE_SDK_APP_ID,
            trtc_secret_key=FAKE_SECRET_KEY,
        )
        self.security = security
        self.validator = CredentialValidator(
            self.store,
            security.owner_credential_hash,
            sidecar_credentials,
            clock=lambda: now,
        )
        self.nonces = NonceService(self.store, ttl_seconds=300)
        self.limiter = RateLimiter(
            self.store,
            RateLimitConfig(window_seconds=60, device_limit=device_limit, ip_limit=ip_limit),
        )
        self.service = RtcSessionService(
            RtcSessionConfig(
                sdk_app_id=FAKE_SDK_APP_ID,
                secret_key=FAKE_SECRET_KEY,
                room_prefix="jax-",
            )
        )
        self.app = FastAPI()
        self.app.include_router(
            create_secured_voice_router(
                store=self.store,
                service=self.service,
                validator=self.validator,
                nonces=self.nonces,
                limiter=self.limiter,
                security=security,
            )
        )
        self.client = TestClient(self.app)

    def session_payload(self, device_id: str) -> dict:
        return {"device_id": device_id, "entry_point": "main"}

    def create_pending_claim(self, device_id: str = DEVICE_A,
                             secret: str = SECRET_A) -> dict:
        created = self.client.post(
            "/api/v1/voice/session",
            json=self.session_payload(device_id),
            headers=self.auth_headers(device_id, secret, nonce()),
        )
        assert created.status_code == 201
        pending = self.client.get(
            "/api/v1/voice/session/pending",
            headers={
                "Authorization": f"Bearer {SIDECAR_SECRET}",
                "X-Request-Nonce": nonce(),
            },
        )
        assert pending.status_code == 200
        return pending.json()["data"]["intents"][0]

    def sign_payload(self, claim: dict, user_id: str = "jax-pc-sidecar") -> dict:
        return {
            "session_id": claim["session_id"],
            "claim_token": claim["claim_token"],
            "device_id": claim["device_id"],
            "user_id": user_id,
        }

    def auth_headers(self, device_id: str, secret: str, nonce: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {device_id}.{secret}"}
        if nonce is not None:
            headers["X-Request-Nonce"] = nonce
        return headers


def nonce() -> str:
    return uuid.uuid4().hex
