"""Business orchestration for issuing and redeeming hello proofs."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .hello_proof import HELLO_KID, HelloProofSigner, verify_hello_proof
from .repositories.hello_proofs import HelloProofRepository


class HelloProofService:
    def __init__(
        self,
        repository: HelloProofRepository,
        signer: HelloProofSigner | None,
        public_key_pem: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._signer = signer
        self._public_key = public_key_pem
        self._clock = clock

    def issue(
        self, context: Mapping[str, Any], *, claim_token_hash: str
    ) -> tuple[dict[str, Any], str]:
        if self._signer is None or not self._public_key:
            raise RuntimeError("hello proof signing unavailable")
        hello = self._signer.issue(context)
        claims = verify_hello_proof(hello["proof"], self._public_key, now=self._clock())
        claims["kid"] = HELLO_KID
        self._repository.create_for_signing(
            claims, hello["proof"], claim_token_hash=claim_token_hash,
            now=self._clock()
        )
        return hello, datetime.fromtimestamp(
            claims["exp"], tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

    def redeem(self, body: Mapping[str, Any]) -> dict[str, Any]:
        claims = verify_hello_proof(
            body["proof"], self._public_key, now=self._clock()
        )
        claims["kid"] = HELLO_KID
        redeemed = self._repository.redeem(
            claims, body["proof"], body, now=self._clock()
        )
        redeemed["expires_at"] = datetime.fromtimestamp(
            redeemed["expires_at"], tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return redeemed
