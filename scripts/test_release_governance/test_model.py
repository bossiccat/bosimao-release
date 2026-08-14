"""RED tests for the release governance claim model (Task 1).

These tests define the wished-for API of scripts.release_governance.model.
Run them first: they must FAIL because the module does not exist yet.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from scripts.release_governance.model import (
    ValidationError,
    attempt_fingerprint,
    validate_claim_shape,
    validate_transition,
    validate_verified_claim,
    validate_cancelled_claim,
)


NOW = datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)


def _min_policy():
    return {
        "schema_version": 1,
        "required_claim_ids": ["windows-popup-free", "android-duplex-audio"],
        "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
        "max_attempts_same_fingerprint": 2,
        "max_evidence_age_hours": 72,
        "required_checks": ["sidecar-verify", "tauri-release-build"],
        "release_channel": "production",
    }


def _verified_claim(**overrides):
    claim = {
        "claim_id": "windows-popup-free",
        "state": "Verified",
        "owner": "impl-team",
        "reviewer": "independent-qa",
        "target": {
            "artifact_commit": "abc123",
            "artifact_sha256": "sha256:deadbeef",
        },
        "evidence": [
            {
                "kind": "windows-field",
                "collected_at": "2026-08-15T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "raw_sha256": "sha256:evidence1",
            }
        ],
    }
    claim.update(overrides)
    return claim


def test_fingerprint_is_stable_and_distinct():
    a1 = attempt_fingerprint("root-cause-A", "method-X", "target-1")
    a2 = attempt_fingerprint("root-cause-A", "method-X", "target-1")
    b = attempt_fingerprint("root-cause-A", "method-Y", "target-1")
    assert a1 == a2
    assert a1 != b


def test_verified_claim_passes_when_fully_bound():
    claim = _verified_claim()
    validate_verified_claim(
        claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
    )


def test_missing_artifact_sha256_rejected():
    claim = _verified_claim()
    del claim["target"]["artifact_sha256"]
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "SHA256" in exc.value.code


def test_commit_mismatch_rejected():
    claim = _verified_claim()
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="different-commit", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "COMMIT" in exc.value.code


def test_expired_evidence_rejected():
    claim = _verified_claim()
    claim["evidence"][0]["expires_at"] = "2026-08-01T00:00:00Z"
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "EXPIRED" in exc.value.code


def test_reviewer_equal_owner_rejected():
    claim = _verified_claim()
    claim["reviewer"] = claim["owner"]
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "REVIEWER" in exc.value.code


def test_p0_claim_must_be_verified():
    claim = _verified_claim(state="EvidencePending")
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "VERIFIED" in exc.value.code


def test_missing_evidence_rejected():
    claim = _verified_claim()
    claim["evidence"] = []
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "EVIDENCE" in exc.value.code


def test_transition_allows_valid_step():
    validate_transition("Draft", "Ready")


def test_transition_rejects_terminal_to_draft():
    with pytest.raises(ValidationError):
        validate_transition("Verified", "Draft")


def test_transition_rejects_skip():
    with pytest.raises(ValidationError):
        validate_transition("Draft", "Verified")


def test_cancelled_requires_superseded_by():
    claim = {
        "claim_id": "windows-popup-free",
        "state": "Cancelled",
        "superseded_by": None,
    }
    with pytest.raises(ValidationError) as exc:
        validate_cancelled_claim(claim)
    assert "SUPERSEDED" in exc.value.code


def test_cancelled_with_superseded_by_passes():
    claim = {
        "claim_id": "windows-popup-free",
        "state": "Cancelled",
        "superseded_by": "windows-popup-free-v2",
    }
    validate_cancelled_claim(claim)


def test_shape_requires_claim_id():
    with pytest.raises(ValidationError):
        validate_claim_shape({"state": "Draft"})


def test_shape_rejects_unknown_state():
    with pytest.raises(ValidationError):
        validate_claim_shape({"claim_id": "x", "state": "NotARealState"})
