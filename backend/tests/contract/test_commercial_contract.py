from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = ROOT / "docs" / "api" / "commercial-voice-openapi.yaml"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
PACKAGE_PATH = ROOT / "pet-ui" / "package.json"
LOCK_PATH = ROOT / "pet-ui" / "package-lock.json"

EXPECTED_OPERATIONS = {
    ("post", "/api/v1/voice/devices/pairing-code"),
    ("post", "/api/v1/voice/devices/register"),
    ("get", "/api/v1/voice/devices"),
    ("post", "/api/v1/voice/devices/{device_id}/revoke"),
    ("post", "/api/v1/voice/session"),
    ("get", "/api/v1/voice/session/pending"),
    ("post", "/api/v1/voice/session/sign"),
    ("get", "/api/v1/voice/status"),
    ("get", "/api/v1/voice/stream"),
}
EXPECTED_ERROR_CODES = {
    40001,
    40101,
    40102,
    40103,
    40401,
    40801,
    40901,
    41301,
    42901,
    50300,
    50301,  # 2026-08-13 新增：termination_unconfirmed（revoke 外部终止未确认，可重试）
    50401,
}
ADRS = range(13, 19)
RANGE_PATTERN = re.compile(r"(?:>=|<=|~=|!=|>|<|\^|~|\*|latest)")

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def _load_openapi() -> dict:
    assert OPENAPI_PATH.is_file(), f"OpenAPI missing: {OPENAPI_PATH}"
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _operation_set(document: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, item in document["paths"].items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def test_openapi_locks_control_plane_capabilities_and_error_codes() -> None:
    document = _load_openapi()
    assert document["openapi"] == "3.0.3"
    assert set(document["paths"]) == {op[1] for op in EXPECTED_OPERATIONS}
    operations = _operation_set(document)
    assert operations == EXPECTED_OPERATIONS
    assert len(operations) == 9
    assert set(document["components"]["schemas"]["ErrorCode"]["enum"]) == EXPECTED_ERROR_CODES


def test_pairing_contract_is_single_use_and_secret_is_one_time() -> None:
    document = _load_openapi()
    pairing = document["paths"]["/api/v1/voice/devices/pairing-code"]["post"]
    description = pairing["description"]
    assert "300" in description
    assert "最多成功消费一次" in description
    max_uses = document["components"]["schemas"]["PairingCodeData"]["properties"]["max_uses"]
    assert max_uses["enum"] == [1]
    ttl = document["components"]["schemas"]["PairingCodeData"]["properties"]["ttl_seconds"]
    assert ttl["type"] == "integer"
    assert ttl["minimum"] == 1
    assert ttl["maximum"] == 300
    secret = document["components"]["schemas"]["RegisteredDeviceData"]["properties"]["credential_secret"]
    assert secret["readOnly"] is True
    assert "只在本次成功响应展示" in secret["description"]


def test_device_bearer_wire_format_binds_device_id_to_one_time_secret() -> None:
    document = _load_openapi()
    scheme = document["components"]["securitySchemes"]["deviceBearer"]

    assert scheme["bearerFormat"] == "<device_id>.<credential_secret>"
    assert "device_id" in scheme["description"]
    assert "credential_secret" in scheme["description"]
    assert "只加密保存 credential_secret" in scheme["description"]


def test_phase_two_adrs_exist() -> None:
    decision_dir = ROOT / "docs" / "decisions"
    for number in ADRS:
        matches = list(decision_dir.glob(f"ADR-{number:03d}-*.md"))
        assert len(matches) == 1, f"expected one ADR-{number:03d}, got {matches}"


def test_python_requirements_are_exact_versions() -> None:
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"Python dependency is not exact: {line}"
        assert RANGE_PATTERN.search(line) is None, line
        assert re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s]+", line), line


def test_frontend_direct_dependencies_match_lockfile_and_have_one_icon_source() -> None:
    package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    root_lock = lock["packages"][""]
    all_direct: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        declared = package.get(section, {})
        assert declared == root_lock.get(section, {})
        all_direct.update(declared)
    for name, version in all_direct.items():
        assert not version.startswith(("^", "~")), f"frontend dependency is ranged: {name}={version}"
        assert version not in {"latest", "*"}
        assert lock["packages"][f"node_modules/{name}"]["version"] == version
    icon_dependencies = {name for name in all_direct if "icon" in name.lower() or "lucide" in name.lower()}
    assert icon_dependencies == {"lucide-react"}
    assert all_direct["lucide-react"] == "0.469.0"
