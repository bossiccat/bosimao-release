"""RED tests for the fail-closed release Claim collection verifier (Task 2)."""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from scripts.release_governance.verify import detect_worktree_clean, verify_claims


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)
EXPECTED_COMMIT = "abc123"
EXPECTED_ARTIFACT_SHA = "sha256:artifact"


def _sha256_file(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _policy():
    return {
        "schema_version": 1,
        "required_claim_ids": ["windows-popup-free", "android-duplex-audio"],
        "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
        "max_attempts_same_fingerprint": 2,
        "max_evidence_age_hours": 72,
        "required_checks": [],
        "release_channel": "production",
    }


def _verified(claim_id, evidence_path, **overrides):
    claim = {
        "claim_id": claim_id,
        "state": "Verified",
        "owner": "impl-team",
        "reviewer": "independent-qa",
        "target": {
            "artifact_commit": EXPECTED_COMMIT,
            "artifact_sha256": EXPECTED_ARTIFACT_SHA,
        },
        "evidence": [
            {
                "kind": "windows-field" if claim_id == "windows-popup-free" else "android-field",
                "collected_at": "2026-08-14T00:00:00Z",
                "expires_at": "2026-08-18T00:00:00Z",
                "path": str(evidence_path),
                "raw_sha256": _sha256_file(evidence_path),
            }
        ],
        "attempts": [],
    }
    claim.update(overrides)
    return claim


def _write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def _write_claims(tmp_path, claims):
    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    for claim in claims:
        _write_json(claims_dir / (claim["claim_id"] + ".json"), claim)
    return claims_dir


def _call(tmp_path, claims, policy=None, **kwargs):
    evidence = tmp_path / "evidence.log"
    evidence.write_text("field evidence", encoding="utf-8")
    prepared = [claim(evidence) if callable(claim) else claim for claim in claims]
    return verify_claims(
        policy or _policy(),
        _write_claims(tmp_path, prepared),
        expected_commit=EXPECTED_COMMIT,
        expected_artifact_sha256=EXPECTED_ARTIFACT_SHA,
        now_utc=NOW,
        worktree_clean=kwargs.get("worktree_clean", True),
    )


def test_passes_for_all_required_verified_claims(tmp_path):
    result = _call(
        tmp_path,
        [
            lambda evidence: _verified("windows-popup-free", evidence),
            lambda evidence: _verified("android-duplex-audio", evidence),
        ],
    )
    assert result == {"verdict": "pass", "errors": []}


def test_required_claim_missing_fails(tmp_path):
    result = _call(tmp_path, [lambda evidence: _verified("windows-popup-free", evidence)])
    assert result["verdict"] == "fail"
    assert any(error["code"] == "MISSING_REQUIRED_CLAIM" for error in result["errors"])


def test_duplicate_claim_id_fails(tmp_path):
    evidence = tmp_path / "evidence.log"
    evidence.write_text("field evidence", encoding="utf-8")
    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    one = _verified("windows-popup-free", evidence)
    two = _verified("windows-popup-free", evidence)
    _write_json(claims_dir / "one.json", one)
    _write_json(claims_dir / "two.json", two)
    result = verify_claims(_policy(), claims_dir, EXPECTED_COMMIT, EXPECTED_ARTIFACT_SHA, NOW, True)
    assert any(error["code"] == "DUPLICATE_CLAIM_ID" for error in result["errors"])


def test_p0_not_verified_fails(tmp_path):
    result = _call(
        tmp_path,
        [
            lambda evidence: _verified("windows-popup-free", evidence, state="EvidencePending"),
            lambda evidence: _verified("android-duplex-audio", evidence),
        ],
    )
    assert any(error["code"] == "NOT_VERIFIED" for error in result["errors"])


def test_evidence_hash_mismatch_fails(tmp_path):
    def bad_hash(evidence):
        claim = _verified("windows-popup-free", evidence)
        claim["evidence"][0]["raw_sha256"] = "sha256:not-the-file"
        return claim

    result = _call(tmp_path, [bad_hash, lambda evidence: _verified("android-duplex-audio", evidence)])
    assert any(error["code"] == "EVIDENCE_SHA256_MISMATCH" for error in result["errors"])


def test_cancelled_claim_without_replacement_fails(tmp_path):
    def cancelled(evidence):
        claim = _verified("windows-popup-free", evidence, state="Cancelled")
        claim.pop("superseded_by", None)
        return claim

    result = _call(tmp_path, [cancelled, lambda evidence: _verified("android-duplex-audio", evidence)])
    assert any(error["code"] == "MISSING_SUPERSEDED_BY" for error in result["errors"])


def test_third_same_failed_fingerprint_fails(tmp_path):
    def repeated(evidence):
        claim = _verified("windows-popup-free", evidence)
        claim["attempts"] = [
            {"fingerprint": "fp:same", "outcome": "failed"},
            {"fingerprint": "fp:same", "outcome": "failed"},
            {"fingerprint": "fp:same", "outcome": "failed"},
        ]
        return claim

    result = _call(tmp_path, [repeated, lambda evidence: _verified("android-duplex-audio", evidence)])
    assert any(error["code"] == "CIRCUIT_OPEN_REQUIRED" for error in result["errors"])


def test_dirty_worktree_fails(tmp_path):
    result = _call(
        tmp_path,
        [
            lambda evidence: _verified("windows-popup-free", evidence),
            lambda evidence: _verified("android-duplex-audio", evidence),
        ],
        worktree_clean=False,
    )
    assert any(error["code"] == "DIRTY_WORKTREE" for error in result["errors"])


def test_detect_worktree_clean_uses_git_status(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.release_governance.verify.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
    )
    assert detect_worktree_clean(tmp_path) is True


def test_detect_worktree_dirty_when_git_reports_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.release_governance.verify.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=" M governance/x.json\n", stderr=""),
    )
    assert detect_worktree_clean(tmp_path) is False
