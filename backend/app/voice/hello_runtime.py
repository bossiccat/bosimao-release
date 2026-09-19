"""Fail-closed construction of control-plane hello proof runtime dependencies."""
from __future__ import annotations

from dataclasses import dataclass

from .auth import CredentialValidator
from .config import ProductionGateError
from .hello_proof import HelloProofSigner
from .hello_service import HelloProofService
from .ingest_ticket import IngestTicketSigner
from .storage import VoiceStore


@dataclass(frozen=True)
class HelloRuntime:
    service: HelloProofService
    certificate_binding: str
    gateway_assertion_hash: str
    # 外带上行 ingest ticket 签发器：与 hello **同一把私钥**，靠 aud 区分凭证类型。
    # 在这里构造而不是另配一个密钥，是为了让「同一把密钥」成为结构性事实而非约定
    # （设计 §1：不引入第二把密钥、不引入新的秘密材料）。
    ingest_signer: IngestTicketSigner


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
        # 同一把 PEM 再构造一个 ingest 签发器（aud 区分）。两者都不在构造期解析
        # 密钥材料，因此装配成功与否只取决于「密钥文本非空」，与既有语义一致。
        ingest_signer = IngestTicketSigner(private_key_pem)
    except Exception as exc:
        raise ProductionGateError("hello signing key configuration invalid") from exc
    return HelloRuntime(
        service=service,
        certificate_binding=certificate_binding,
        gateway_assertion_hash=CredentialValidator.hash_credential(gateway_assertion),
        ingest_signer=ingest_signer,
    )
