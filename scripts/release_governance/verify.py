"""Fail-closed validation of a complete release Claim collection."""

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.release_governance.model import (
    ValidationError,
    validate_cancelled_claim,
    validate_claim_shape,
    validate_verified_claim,
)


def _error(code, message, claim_id=None):
    record = {"code": code, "message": message}
    if claim_id:
        record["claim_id"] = claim_id
    return record


def _sha256_file(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def detect_worktree_clean(repo_root):
    """Return False if git cannot prove the requested tree is clean."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stdout.strip()


def _load_claims(claims_dir):
    claims = []
    errors = []
    base = Path(claims_dir)
    if not base.is_dir():
        return [], [_error("CLAIMS_DIRECTORY_MISSING", "claims directory does not exist")]
    for path in sorted(base.glob("*.json")):
        try:
            claims.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(_error("INVALID_CLAIM_JSON", "%s: %s" % (path, exc)))
    return claims, errors


def _validate_evidence_hashes(claim):
    errors = []
    for evidence in claim.get("evidence") or []:
        source = evidence.get("path")
        expected = evidence.get("raw_sha256") or evidence.get("result_sha256")
        if not source or not expected:
            continue
        path = Path(source)
        if not path.is_file():
            errors.append(
                _error("EVIDENCE_FILE_MISSING", "evidence path is missing: %s" % source, claim.get("claim_id"))
            )
        elif _sha256_file(path) != expected:
            errors.append(
                _error("EVIDENCE_SHA256_MISMATCH", "evidence SHA-256 does not match %s" % source, claim.get("claim_id"))
            )
    return errors


def _validate_attempt_circuit(claim, max_attempts):
    counts = {}
    for attempt in claim.get("attempts") or []:
        if attempt.get("outcome") != "failed":
            continue
        fingerprint = attempt.get("fingerprint")
        if fingerprint:
            counts[fingerprint] = counts.get(fingerprint, 0) + 1
    if any(count > max_attempts for count in counts.values()):
        if claim.get("state") not in {"CircuitOpen", "Escalated"}:
            return [
                _error(
                    "CIRCUIT_OPEN_REQUIRED",
                    "same failure fingerprint exceeded max attempts without CircuitOpen/Escalated",
                    claim.get("claim_id"),
                )
            ]
    return []


def verify_claims(policy, claims_dir, expected_commit, expected_artifact_sha256, now_utc, worktree_clean):
    """Return {verdict: pass|fail, errors: [...]} without raising for bad inputs."""
    errors = []
    if not worktree_clean:
        errors.append(_error("DIRTY_WORKTREE", "release verification requires a clean worktree"))

    claims, load_errors = _load_claims(claims_dir)
    errors.extend(load_errors)
    by_id = {}
    for claim in claims:
        claim_id = claim.get("claim_id") if isinstance(claim, dict) else None
        if claim_id in by_id:
            errors.append(_error("DUPLICATE_CLAIM_ID", "duplicate claim_id %s" % claim_id, claim_id))
            continue
        if claim_id:
            by_id[claim_id] = claim
        try:
            validate_claim_shape(claim)
            validate_cancelled_claim(claim)
            if claim_id in policy.get("required_claim_ids", []):
                validate_verified_claim(
                    claim, policy, now_utc, expected_commit, expected_artifact_sha256
                )
            errors.extend(_validate_evidence_hashes(claim))
            errors.extend(
                _validate_attempt_circuit(
                    claim, policy.get("max_attempts_same_fingerprint", 2)
                )
            )
        except ValidationError as exc:
            errors.append(_error(exc.code, exc.message, claim_id))

    for required_id in policy.get("required_claim_ids", []):
        if required_id not in by_id:
            errors.append(
                _error("MISSING_REQUIRED_CLAIM", "required claim is missing: %s" % required_id, required_id)
            )

    return {"verdict": "fail" if errors else "pass", "errors": errors}
