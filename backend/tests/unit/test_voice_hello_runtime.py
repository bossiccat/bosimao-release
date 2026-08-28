from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.voice.config import ProductionGateError
from app.voice.hello_runtime import build_hello_runtime
from app.voice.storage import VoiceStore


def _keys() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


def test_production_hello_runtime_rejects_each_missing_security_binding(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    private_pem, public_pem = _keys()
    complete = {
        "private_key_pem": private_pem,
        "public_key_pem": public_pem,
        "rtc_bridge_credential": "bridge-service-secret",
        "certificate_binding": "sha256:gateway-derived-binding",
        "gateway_assertion": "gateway-shared-assertion-secret",
    }
    for missing in complete:
        config = {**complete, missing: ""}
        with pytest.raises(ProductionGateError):
            build_hello_runtime(store=store, production=True, **config)


def test_complete_hello_runtime_builds_signing_service_and_binding(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    private_pem, public_pem = _keys()
    runtime = build_hello_runtime(
        store=store,
        production=True,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        rtc_bridge_credential="bridge-service-secret",
        certificate_binding="sha256:gateway-derived-binding",
        gateway_assertion="gateway-shared-assertion-secret",
    )
    assert runtime.service is not None
    assert runtime.certificate_binding == "sha256:gateway-derived-binding"
    assert runtime.gateway_assertion_hash
