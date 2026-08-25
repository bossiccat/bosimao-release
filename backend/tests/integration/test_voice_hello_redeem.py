from __future__ import annotations

import copy
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice_hello import build_hello_router
from app.voice.auth import CredentialValidator
from app.voice.control_plane import SessionLedger
from app.voice.hello_proof import HelloProofSigner
from app.voice.hello_service import HelloProofService
from app.voice.storage import VoiceStore
from app.voice.trusted_gateway import TrustedGatewayIdentityMiddleware

RTC_SECRET = "rtc-bridge-secret-0123456789abcdef"
CERT_BINDING = "ab" * 32
GATEWAY_ASSERTION = "gateway-shared-assertion-secret"
SESSION = "00000000-0000-4000-8000-000000000001"
DEVICE = "00000000-0000-4000-8000-000000000002"


def _keys() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _fixture(tmp_path: Path):
    now = [1_800_000_000.0]
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE, "device-secret-0123456789abcdef")
    ledger = SessionLedger(store)
    ledger.create_session(
        session_id=SESSION, device_id=DEVICE, room_id="room-current", generation=13,
        state="SIGNING",
    )
    store.enqueue_pending_session(
        SESSION, DEVICE, "room-current", 13, now[0] + 300, now=now[0]
    )
    claim = store.claim_pending_session(now=now[0])
    private_pem, public_pem = _keys()
    service = HelloProofService(
        store.hello_proofs,
        HelloProofSigner(private_pem, clock=lambda: now[0]),
        public_pem,
        clock=lambda: now[0],
    )
    hello, _ = service.issue(
        {
            "session_id": SESSION,
            "device_id": DEVICE,
            "room_id": "room-current",
            "sidecar_user_id": "jax-pc-sidecar",
            "generation": 13,
        },
        claim_token_hash=store.pending_sessions._hash_token(claim["claim_token"]),
    )
    validator = CredentialValidator(
        store,
        rtc_bridge_credential_hash=CredentialValidator.hash_credential(RTC_SECRET),
    )
    app = FastAPI()
    app.include_router(build_hello_router(
        validator=validator,
        service=service,
        expected_certificate_binding=CERT_BINDING,
    ))
    trusted_app = TrustedGatewayIdentityMiddleware(
        app,
        gateway_assertion_hash=CredentialValidator.hash_credential(GATEWAY_ASSERTION),
        certificate_binding=CERT_BINDING,
        allowed_hosts={"testclient"},
    )
    return now, store, TestClient(trusted_app), TestClient(app), hello


def _headers(secret: str = RTC_SECRET, *, verified: bool = True) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret}",
        "X-Client-Certificate-Thumbprint": CERT_BINDING,
        "X-Internal-Gateway-Assertion": GATEWAY_ASSERTION if verified else "invalid",
    }


def test_first_redeem_activates_session_and_retry_is_replay(tmp_path: Path) -> None:
    _now, store, client, _external_client, hello = _fixture(tmp_path)
    first = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=hello, headers=_headers()
    )
    assert first.status_code == 200
    assert first.json()["data"] == {
        "redeemed": True,
        "session_id": SESSION,
        "device_id": DEVICE,
        "room_id": "room-current",
        "sidecar_user_id": "jax-pc-sidecar",
        "generation": 13,
        "expires_at": "2027-01-15T08:01:00Z",
    }
    assert "proof" not in first.text and "nonce" not in first.text
    assert SessionLedger(store).get_session(SESSION)["state"] == "ACTIVE"

    replay = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=hello, headers=_headers()
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == 40113


def test_redeem_error_boundaries_are_fail_closed(tmp_path: Path) -> None:
    now, store, client, _external_client, hello = _fixture(tmp_path)

    no_service = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=hello, headers={}
    )
    assert no_service.status_code == 401 and no_service.json()["code"] == 40114
    no_mtls = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem",
        json=hello, headers=_headers(verified=False),
    )
    assert no_mtls.status_code == 401 and no_mtls.json()["code"] == 40114
    forged_legacy_headers = {
        "Authorization": f"Bearer {RTC_SECRET}",
        "X-Client-Certificate-Thumbprint": CERT_BINDING,
        "X-Client-Certificate-Verified": "true",
    }
    forged = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem",
        json=hello, headers=forged_legacy_headers,
    )
    assert forged.status_code == 401 and forged.json()["code"] == 40114

    wrong_identity = copy.deepcopy(hello)
    wrong_identity["room_id"] = "wrong-room"
    invalid = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem",
        json=wrong_identity, headers=_headers(),
    )
    assert invalid.status_code == 401 and invalid.json()["code"] == 40111

    wrong_nonce = copy.deepcopy(hello)
    wrong_nonce["nonce"] = "x" * 32
    replay = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=wrong_nonce, headers=_headers()
    )
    assert replay.status_code == 401 and replay.json()["code"] == 40113

    now[0] += 60
    expired = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=hello, headers=_headers()
    )
    assert expired.status_code == 401 and expired.json()["code"] == 40112

    now[0] -= 60
    with store.connect() as conn:
        conn.execute(
            "UPDATE control_plane_sessions SET generation = 14 WHERE session_id = ?",
            (SESSION,),
        )
        conn.commit()
    conflict = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=hello, headers=_headers()
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == 40914


def test_external_request_cannot_self_assert_gateway_identity(tmp_path: Path) -> None:
    _now, _store, _trusted_client, external_client, hello = _fixture(tmp_path)
    response = external_client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem",
        json=hello,
        headers=_headers(),
    )
    assert response.status_code == 401
    assert response.json()["code"] == 40114


def test_extra_or_missing_field_returns_frozen_invalid_hello(tmp_path: Path) -> None:
    _now, _store, client, _external_client, hello = _fixture(tmp_path)
    invalid = dict(hello)
    invalid["role"] = "sidecar"
    response = client.post(
        "/api/v1/voice/internal/rtc-bridge/hello-redeem", json=invalid, headers=_headers()
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40021
