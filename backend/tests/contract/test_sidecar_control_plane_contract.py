"""Contract checks for sidecar security and pending control-plane endpoint."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
OPENAPI = ROOT.parent / "docs" / "api" / "commercial-voice-openapi.yaml"


def test_openapi_defines_protected_atomic_pending_endpoint() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    operation = document["paths"]["/api/v1/voice/session/pending"]["get"]
    assert operation["security"] == [{"sidecarBearer": []}]
    names = {parameter["$ref"] for parameter in operation["parameters"]}
    assert "#/components/parameters/RequestNonce" in names
    assert "#/components/schemas/PendingSessionResponse" in str(operation)
    assert "audio" not in str(operation).lower()


def test_pending_response_is_control_metadata_only() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["PendingSessionData"]
    assert set(schema["required"]) >= {"intents"}
    serialized = str(schema).lower()
    assert "pcm" not in serialized
    assert "audio" not in serialized
