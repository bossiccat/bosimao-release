from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.voice.hello_proof import (
    AUDIO_FORMAT,
    HELLO_KID,
    HelloProofError,
    HelloProofSigner,
    verify_hello_proof,
)


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


def _context() -> dict:
    return {
        "session_id": "00000000-0000-4000-8000-000000000001",
        "device_id": "00000000-0000-4000-8000-000000000002",
        "room_id": "room-current",
        "sidecar_user_id": "jax-pc-sidecar",
        "generation": 13,
    }


def _header(proof: str) -> dict:
    encoded = proof.split(".", 1)[0]
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded))


def test_signer_emits_exact_header_claims_ttl_and_audio_contract() -> None:
    private_pem, public_pem = _keys()
    hello = HelloProofSigner(private_pem, clock=lambda: 1_800_000_000).issue(_context())

    assert set(hello) == {
        "type", "proof", "nonce", "jti", "session_id", "device_id", "room_id",
        "sidecar_user_id", "generation", "protocol_version", "audio_format",
    }
    assert _header(hello["proof"]) == {"alg": "EdDSA", "kid": HELLO_KID, "typ": "JWT"}
    assert len(base64.urlsafe_b64decode(hello["nonce"] + "=" * (-len(hello["nonce"]) % 4))) >= 16
    assert hello["audio_format"] == AUDIO_FORMAT

    claims = verify_hello_proof(hello["proof"], public_pem, now=1_800_000_000)
    assert claims["iss"] == "commercial-control-plane"
    assert claims["aud"] == "rtc_bridge"
    assert claims["iat"] == 1_800_000_000
    assert claims["exp"] == 1_800_000_060
    for key, value in hello.items():
        if key not in {"type", "proof"}:
            assert claims[key] == value


def test_verifier_rejects_tamper_expiry_and_future_iat_without_clock_leeway() -> None:
    private_pem, public_pem = _keys()
    hello = HelloProofSigner(private_pem, clock=lambda: 1_800_000_000).issue(_context())
    prefix, payload, signature = hello["proof"].split(".")
    tampered = ".".join((prefix, payload[:-1] + ("A" if payload[-1] != "A" else "B"), signature))

    with pytest.raises(HelloProofError) as invalid:
        verify_hello_proof(tampered, public_pem, now=1_800_000_000)
    assert invalid.value.code == 40111

    with pytest.raises(HelloProofError) as expired:
        verify_hello_proof(hello["proof"], public_pem, now=1_800_000_060)
    assert expired.value.code == 40112

    with pytest.raises(HelloProofError) as future:
        verify_hello_proof(hello["proof"], public_pem, now=1_799_999_999)
    assert future.value.code == 40112


def test_verifier_rejects_non_exact_protected_header() -> None:
    private_pem, public_pem = _keys()
    signer = HelloProofSigner(private_pem, clock=lambda: 1_800_000_000)
    hello = signer.issue(_context(), extra_headers={"cty": "JWT"})
    with pytest.raises(HelloProofError) as rejected:
        verify_hello_proof(hello["proof"], public_pem, now=1_800_000_000)
    assert rejected.value.code == 40111
