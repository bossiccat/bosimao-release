"""Ed25519 signed, one-time commercial stream hello proofs."""
from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import jwt

HELLO_KID = "cp-hello-ed25519-v1"
ISSUER = "commercial-control-plane"
AUDIENCE = "rtc_bridge"
PROTOCOL_VERSION = "1.0"
PROOF_TTL_SECONDS = 60
AUDIO_FORMAT = {
    "encoding": "pcm_s16le",
    "sample_rate_hz": 16000,
    "channels": 1,
    "frame_ms": 20,
    "frame_bytes": 640,
}
_HEADER = {"alg": "EdDSA", "typ": "JWT", "kid": HELLO_KID}
_IDENTITY = (
    "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
)
_REQUIRED_CLAIMS = {
    "iss", "aud", "jti", *_IDENTITY, "nonce", "protocol_version",
    "audio_format", "iat", "exp",
}


class HelloProofError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("hello proof rejected")
        self.code = code


class HelloProofSigner:
    def __init__(self, private_key_pem: str, *, clock: Callable[[], float] = time.time) -> None:
        if not private_key_pem:
            raise ValueError("hello proof private key unavailable")
        self._private_key = private_key_pem
        self._clock = clock

    def issue(
        self,
        context: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if any(key not in context for key in _IDENTITY):
            raise ValueError("hello proof context incomplete")
        issued_at = int(self._clock())
        nonce = secrets.token_urlsafe(24)
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "jti": str(uuid.uuid4()),
            **{key: context[key] for key in _IDENTITY},
            "nonce": nonce,
            "protocol_version": PROTOCOL_VERSION,
            "audio_format": dict(AUDIO_FORMAT),
            "iat": issued_at,
            "exp": issued_at + PROOF_TTL_SECONDS,
        }
        headers = dict(_HEADER)
        if extra_headers:
            headers.update(extra_headers)
        proof = jwt.encode(claims, self._private_key, algorithm="EdDSA", headers=headers)
        return {
            "type": "hello",
            "proof": proof,
            "nonce": nonce,
            "jti": claims["jti"],
            **{key: claims[key] for key in _IDENTITY},
            "protocol_version": PROTOCOL_VERSION,
            "audio_format": dict(AUDIO_FORMAT),
        }


def verify_hello_proof(proof: str, public_key_pem: str, *, now: float | None = None) -> dict[str, Any]:
    if not proof or not public_key_pem:
        raise HelloProofError(40111)
    try:
        if jwt.get_unverified_header(proof) != _HEADER:
            raise HelloProofError(40111)
        claims = jwt.decode(
            proof,
            public_key_pem,
            algorithms=["EdDSA"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"verify_exp": False, "verify_iat": False, "require": list(_REQUIRED_CLAIMS)},
        )
    except HelloProofError:
        raise
    except jwt.PyJWTError as exc:
        raise HelloProofError(40111) from exc

    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    current = int(time.time() if now is None else now)
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at != issued_at + PROOF_TTL_SECONDS
        or issued_at > current
        or expires_at <= current
    ):
        raise HelloProofError(40112)
    if claims.get("protocol_version") != PROTOCOL_VERSION:
        raise HelloProofError(40111)
    if claims.get("audio_format") != AUDIO_FORMAT:
        raise HelloProofError(40111)
    if not isinstance(claims.get("generation"), int) or claims["generation"] < 0:
        raise HelloProofError(40111)
    if any(not isinstance(claims.get(key), str) or not claims[key] for key in (*_IDENTITY[:4], "jti", "nonce")):
        raise HelloProofError(40111)
    return claims
