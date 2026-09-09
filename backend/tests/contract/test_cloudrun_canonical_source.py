from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CLOUDAPI = ROOT / "cloudapi"
WORKFLOW = ROOT / ".github" / "workflows" / "release-governance.yml"


REQUIRED_SOURCE_PATHS = (
    "cloudapi/CANONICAL_SOURCE.json",
    "cloudapi/Dockerfile",
    "cloudapi/main.py",
    "cloudapi/requirements.txt",
    "cloudbase/migrations",
)


def test_jax_voice_api_manifest_declares_one_tracked_cloudrun_source() -> None:
    manifest_path = CLOUDAPI / "CANONICAL_SOURCE.json"
    assert manifest_path.is_file(), "jax-voice-api canonical source manifest is missing"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["service"] == "jax-voice-api"
    assert manifest["build_context"] == "."
    assert manifest["dockerfile"] == "cloudapi/Dockerfile"
    assert manifest["entrypoint"] == "cloudapi/main.py"
    assert set(manifest["required_paths"]) == set(REQUIRED_SOURCE_PATHS)
    assert "deploy/backend" in manifest["forbidden_paths"]

    for relative_path in manifest["required_paths"]:
        assert (ROOT / relative_path).exists(), relative_path


def test_cloudrun_dockerfile_has_no_sqlite_runtime_storage() -> None:
    dockerfile = (CLOUDAPI / "Dockerfile").read_text(encoding="utf-8").lower()
    assert "voice_db_path" not in dockerfile
    assert "/data/voice.db" not in dockerfile
    assert "volume [\"/data\"]" not in dockerfile
    assert "sqlite" not in dockerfile


def test_cloudrun_entrypoint_has_postgres_fail_closed_gate_before_store() -> None:
    source = (CLOUDAPI / "main.py").read_text(encoding="utf-8")
    gate_index = source.index("validate_voice_storage")
    production_failure_index = source.index("production PostgreSQL adapter")
    store_index = source.index("VoiceStore")
    assert gate_index < production_failure_index < store_index
    assert "storage_backend=settings.voice_storage_backend" in source
    assert "database_url=settings.voice_database_url" in source


def test_release_workflow_verifies_canonical_source_is_in_candidate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for relative_path in REQUIRED_SOURCE_PATHS:
        assert f"git ls-files --error-unmatch {relative_path}" in workflow
    assert "deploy/backend" in workflow
    assert "forbidden" in workflow.lower()
