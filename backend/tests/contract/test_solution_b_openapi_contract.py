from __future__ import annotations

from pathlib import Path
import warnings

warnings.filterwarnings(
    "ignore",
    message="jsonschema.RefResolver is deprecated*",
    category=DeprecationWarning,
)

import pytest
import yaml
from jsonschema import RefResolver
from openapi_schema_validator import OAS30Validator

ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = ROOT / "docs" / "api" / "commercial-voice-openapi.yaml"
OAS30_SECURITY_SCHEME_TYPES = {"apiKey", "http", "oauth2", "openIdConnect"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
HELLO_REQUIRED = {
    "type",
    "proof",
    "nonce",
    "jti",
    "session_id",
    "device_id",
    "room_id",
    "sidecar_user_id",
    "generation",
    "protocol_version",
    "audio_format",
}
ACKNOWLEDGEMENTS = {
    "android_trtc_left",
    "sidecar_trtc_left",
    "bridge_drained_closed",
    "apm_cancelled_closed",
    "brain_turns_sealed",
}


def _load_openapi() -> dict:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_openapi_contract_toolchain_is_exactly_pinned() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    declared = {line.strip() for line in requirements}
    assert {
        "jsonschema==4.26.0",
        "openapi-schema-validator==0.9.0",
        "openapi-spec-validator==0.9.0",
    } <= declared


def test_global_operation_ids_local_refs_and_frozen_p0_contracts() -> None:
    document = _load_openapi()
    operations = [
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method.lower() in HTTP_METHODS
    ]
    operation_ids = [operation["operationId"] for operation in operations]
    assert len(operations) == 15
    assert len(operation_ids) == len(set(operation_ids))

    # ack 上报端点：reporter 由 credential 派生，不得出现在请求体
    ack = document["paths"][
        "/api/v1/voice/sessions/{session_id}/termination/{termination_id}/acknowledgements"
    ]["post"]
    assert ack["x-reporter-derivation"] == {
        "deviceBearer": "android",
        "sidecarBearer": "sidecar",
        "rtcBridgeService": "rtc_bridge",
        "brainService": "brain",
    }
    ack_request = document["components"]["schemas"]["AckReportRequest"]
    assert "reporter" not in ack_request["properties"]
    assert ack["security"] == [
        {"deviceBearer": []},
        {"sidecarBearer": []},
        {"rtcBridgeService": []},
        {"brainService": []},
    ]

    for node in _walk(document):
        ref = node.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/"):
            continue
        target: object = document
        for segment in ref[2:].split("/"):
            assert isinstance(target, dict) and segment in target, f"unresolved $ref: {ref}"
            target = target[segment]

    schemas = document["components"]["schemas"]
    sign_request = schemas["CreateSidecarSessionRequest"]
    assert set(sign_request["required"]) == {
        "session_id", "claim_token", "device_id", "user_id"
    }
    assert sign_request["additionalProperties"] is False
    session_data = schemas["SessionData"]
    assert {"generation", "hello", "hello_expires_at"} <= set(session_data["required"])
    assert session_data["properties"]["hello"] == {
        "$ref": "#/components/schemas/CommercialStreamHello"
    }
    hello = schemas["CommercialStreamHello"]
    assert set(hello["required"]) == HELLO_REQUIRED
    assert schemas["HelloRedeemRequest"] == {
        "allOf": [{"$ref": "#/components/schemas/CommercialStreamHello"}]
    }
    assert schemas["WakeSessionData"]["properties"]["user_sig"]["readOnly"] is True
    assert schemas["SessionData"]["properties"]["user_sig"]["readOnly"] is True

    redeem = document["paths"][
        "/api/v1/voice/internal/rtc-bridge/hello-redeem"
    ]["post"]
    assert redeem["x-error-codes"] == [40914, 50303]

    confirmed = schemas["ConfirmedTerminationAcknowledgements"]
    assert set(confirmed["required"]) == ACKNOWLEDGEMENTS
    assert all(value["enum"] == ["confirmed"] for value in confirmed["properties"].values())
    for name in ("RootTerminationCompleteData", "RetryTerminationCompleteData"):
        branch = schemas[name]
        assert branch["properties"]["acknowledgements"] == {
            "$ref": "#/components/schemas/ConfirmedTerminationAcknowledgements"
        }
        terminal_at = branch["properties"]["terminal_at"]
        assert terminal_at == {"type": "string", "format": "date-time"}


def test_oas30_nullable_terminal_result_accepts_null_and_rejects_unknown_values() -> None:
    document = _load_openapi()
    terminal_result = document["components"]["schemas"]["VoiceStatusData"]["properties"][
        "terminal_result"
    ]
    assert terminal_result == {
        "type": "string",
        "nullable": True,
        "enum": ["complete", "partial", "timeout", None],
    }

    validator = OAS30Validator(terminal_result)
    for value in (None, "complete", "partial", "timeout"):
        validator.validate(value)
    with pytest.raises(Exception):
        validator.validate("pending")

    for node in _walk(document):
        if "$ref" in node:
            assert set(node) == {"$ref"}, f"OAS 3.0 ignores $ref siblings: {node}"


def test_oas30_security_schemes_are_valid_and_mtls_is_gateway_enforced() -> None:
    document = _load_openapi()
    assert document["openapi"] == "3.0.3"

    schemes = document["components"]["securitySchemes"]
    assert {scheme["type"] for scheme in schemes.values()} <= OAS30_SECURITY_SCHEME_TYPES

    operation = document["paths"][
        "/api/v1/voice/internal/rtc-bridge/hello-redeem"
    ]["post"]
    assert operation["security"] == [{"rtcBridgeService": []}]
    enforcement = operation["x-mtls-enforcement"]
    assert enforcement["required"] is True
    assert enforcement["certificate_binding"] == "rtcBridgeService.subject"
    assert enforcement["strip_untrusted_certificate_header"] is True
    assert enforcement["fail_closed"] is True

    certificate_header = document["components"]["parameters"]["RtcBridgeClientCertificate"]
    assert certificate_header["x-gateway-derived"] is True
    assert certificate_header["x-client-supplied"] is False


def test_termination_status_branches_are_mutually_exclusive_for_root_and_retry() -> None:
    document = _load_openapi()
    schemas = document["components"]["schemas"]
    status = schemas["TerminationStatusData"]

    assert status["discriminator"]["propertyName"] == "status_variant"
    assert len(status["oneOf"]) == 8

    branch_names = {ref["$ref"].rsplit("/", 1)[-1] for ref in status["oneOf"]}
    assert branch_names == {
        "RootTerminationPendingData",
        "RootTerminationCompleteData",
        "RootTerminationPartialData",
        "RootTerminationTimeoutData",
        "RetryTerminationPendingData",
        "RetryTerminationCompleteData",
        "RetryTerminationPartialData",
        "RetryTerminationTimeoutData",
    }
    expected_mapping = {
        "root_pending": "RootTerminationPendingData",
        "root_complete": "RootTerminationCompleteData",
        "root_partial": "RootTerminationPartialData",
        "root_timeout": "RootTerminationTimeoutData",
        "retry_child_pending": "RetryTerminationPendingData",
        "retry_child_complete": "RetryTerminationCompleteData",
        "retry_child_partial": "RetryTerminationPartialData",
        "retry_child_timeout": "RetryTerminationTimeoutData",
    }
    assert status["discriminator"]["mapping"] == {
        variant: f"#/components/schemas/{name}" for variant, name in expected_mapping.items()
    }

    for name in branch_names:
        branch = schemas[name]
        required = set(branch["required"])
        properties = branch["properties"]
        assert {"scope", "status_variant"} <= required
        expected_scope = "retry_child" if name.startswith("Retry") else "root"
        expected_result = name.removeprefix("RetryTermination").removeprefix(
            "RootTermination"
        ).removesuffix("Data").lower()
        assert properties["scope"] == {"type": "string", "enum": [expected_scope]}
        assert properties["status_variant"] == {
            "type": "string",
            "enum": [f"{expected_scope}_{expected_result}"],
        }
        if expected_scope == "retry_child":
            assert "parent_termination_id" in required
            assert properties["parent_termination_id"]["minLength"] == 1
        else:
            assert "parent_termination_id" not in properties

        variant = properties["status_variant"]["enum"][0]
        instance = {
            "status_variant": variant,
            "scope": expected_scope,
            "result": expected_result,
        }
        matching_branches = [
            candidate
            for candidate in branch_names
            if instance["status_variant"]
            in schemas[candidate]["properties"]["status_variant"]["enum"]
        ]
        assert matching_branches == [name]

    response_data = schemas["TerminationStatusResponse"]["properties"]["data"]
    assert response_data == {"$ref": "#/components/schemas/TerminationStatusData"}
    assert "RetryTerminationStatusData" not in schemas


def test_termination_branches_have_exactly_one_valid_instance() -> None:
    document = _load_openapi()
    schemas = document["components"]["schemas"]
    status = schemas["TerminationStatusData"]
    resolver = RefResolver.from_schema(document)
    branch_refs = [ref["$ref"] for ref in status["oneOf"]]
    branch_names = [ref.rsplit("/", 1)[-1] for ref in branch_refs]

    for index, branch_name in enumerate(branch_names, start=1):
        is_retry = branch_name.startswith("Retry")
        result = branch_name.removeprefix("RetryTermination").removeprefix(
            "RootTermination"
        ).removesuffix("Data").lower()
        scope = "retry_child" if is_retry else "root"
        status_variant = f"{scope}_{result}"
        instance = {
            "type": "session.terminating" if result == "pending" else "session.terminated",
            "scope": scope,
            "status_variant": status_variant,
            "termination_id": f"00000000-0000-4000-8000-{index:012d}",
            "session_id": f"00000000-0000-4000-8000-{index + 100:012d}",
            "generation": 3,
            "result": result,
            "acknowledgements": {
                name: "pending" if result == "pending" else "confirmed"
                for name in ACKNOWLEDGEMENTS
            },
            "terminal_at": None if result == "pending" else "2026-08-23T10:00:00Z",
            "retryable": result in {"partial", "timeout"},
        }
        if is_retry:
            instance["parent_termination_id"] = "00000000-0000-4000-8000-999999999999"

        matches = []
        for ref in branch_refs:
            with resolver.resolving(ref) as schema:
                try:
                    OAS30Validator(schema, resolver=resolver).validate(instance)
                except Exception:
                    continue
                matches.append(ref)
        assert matches == [f"#/components/schemas/{branch_name}"]

    complete = {
        "type": "session.terminated",
        "scope": "root",
        "status_variant": "root_complete",
        "termination_id": "00000000-0000-4000-8000-000000000001",
        "session_id": "00000000-0000-4000-8000-000000000002",
        "generation": 3,
        "result": "complete",
        "acknowledgements": {name: "confirmed" for name in ACKNOWLEDGEMENTS},
        "terminal_at": "2026-08-23T10:00:00Z",
        "retryable": False,
    }
    with resolver.resolving("#/components/schemas/RootTerminationCompleteData") as schema:
        invalid_ack = dict(complete)
        invalid_ack["acknowledgements"] = dict(complete["acknowledgements"])
        invalid_ack["acknowledgements"]["apm_cancelled_closed"] = "failed"
        with pytest.raises(Exception):
            OAS30Validator(schema, resolver=resolver).validate(invalid_ack)

        invalid_terminal = dict(complete)
        invalid_terminal["terminal_at"] = None
        with pytest.raises(Exception):
            OAS30Validator(schema, resolver=resolver).validate(invalid_terminal)
