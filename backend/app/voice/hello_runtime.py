"""Fail-closed construction of control-plane hello proof runtime dependencies."""
from __future__ import annotations

from dataclasses import dataclass

from .auth import CredentialValidator
from .config import ProductionGateError
from .hello_proof import HelloProofSigner
from .hello_service import HelloProofService
from .storage import VoiceStore


@dataclass(frozen=True)
class HelloRuntime:
    service: HelloProofService
    certificate_binding: str
    gateway_assertion_hash: str


def build_hello_runtime(
    *,
    store: VoiceStore,
    production: bool,
    private_key_pem: str,
    public_key_pem: str,
    rtc_bridge_credential: str,
    certificate_binding: str,
    gateway_assertion: str,
) -> HelloRuntime:
    required = {
        "private_key_pem": private_key_pem,
        "public_key_pem": public_key_pem,
        "rtc_bridge_credential": rtc_bridge_credential,
        "certificate_binding": certificate_binding,
        "gateway_assertion": gateway_assertion,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        mode = "production" if production else "runtime"
        raise ProductionGateError(f"{mode} hello security capability missing: {', '.join(missing)}")
    try:
        signer = HelloProofSigner(private_key_pem)
        service = HelloProofService(store.hello_proofs, signer, public_key_pem)
    except Exception as exc:
        raise ProductionGateError("hello signing key configuration invalid") from exc
    return HelloRuntime(
        service=service,
        certificate_binding=certificate_binding,
        gateway_assertion_hash=CredentialValidator.hash_credential(gateway_assertion),
    )
