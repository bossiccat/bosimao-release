"""Fail-closed validation of a complete release Claim collection."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

FULL_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")

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


def _run_git(repo_root, *args):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _allowed_delta_prefixes(repo_root, claims_dir):
    """artifact_commit..HEAD 之间允许出现的改动前缀。

    claims 目录在仓库内 ⇒ 只允许该目录；在仓库外（例如证据仓/临时夹具）⇒ 不允许任何改动，
    此时血缘判据退化为“HEAD 必须就是 artifact_commit”，即历史行为。
    """
    if repo_root is None:
        return ()
    try:
        relative = Path(claims_dir).resolve().relative_to(Path(repo_root).resolve())
    except ValueError:
        return ()
    return (relative.as_posix(),)


def commit_lineage(repo_root, artifact_commit, head_commit, allowed_prefixes):
    """返回 (proof, error)。proof 是给 model.validate_verified_claim 的血缘证明。"""
    if not FULL_COMMIT_PATTERN.match(str(artifact_commit or "")):
        return None, _error(
            "COMMIT_MISMATCH", "artifact_commit must be a full lowercase SHA-1"
        )
    ancestor = _run_git(repo_root, "merge-base", "--is-ancestor", artifact_commit, head_commit)
    if ancestor is None or ancestor.returncode != 0:
        return None, _error(
            "COMMIT_LINEAGE_INVALID",
            "artifact_commit %s is not an ancestor of HEAD %s" % (artifact_commit, head_commit),
        )
    diff = _run_git(repo_root, "diff", "--name-only", "--no-renames", artifact_commit, head_commit)
    if diff is None or diff.returncode != 0:
        return None, _error(
            "COMMIT_LINEAGE_INVALID", "cannot enumerate the delta between artifact_commit and HEAD"
        )
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    offending = [
        path
        for path in changed
        if not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed_prefixes)
    ]
    if offending:
        return None, _error(
            "COMMIT_LINEAGE_INVALID",
            "changes between artifact_commit and HEAD are not confined to claims: %s"
            % ", ".join(offending),
        )
    return (
        {
            "artifact_commit": artifact_commit,
            "head_commit": head_commit,
            "is_ancestor": True,
            "claims_only_delta": True,
            "changed_paths": changed,
        },
        None,
    )


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


def verify_claims(
    policy,
    claims_dir,
    expected_commit,
    expected_artifact_sha256,
    now_utc,
    worktree_clean,
    repo_root=None,
):
    """Return {verdict: pass|fail, errors: [...]} without raising for bad inputs.

    repo_root=None 保持历史语义（artifact_commit 必须精确等于 expected_commit）；
    给出 repo_root 时改用血缘判据（见 _allowed_delta_prefixes）。
    """
    errors = []
    if not worktree_clean:
        errors.append(_error("DIRTY_WORKTREE", "release verification requires a clean worktree"))

    allowed_prefixes = _allowed_delta_prefixes(repo_root, claims_dir)
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
                lineage = None
                # 只在 claim 已是 Verified 时才校验 target 的提交血缘。
                # 否则本检查会抢在 `validate_verified_claim` 的 NOT_VERIFIED（model.py:119-123）
                # 之前 raise，把"这条 claim 根本还没归档"这个**真实阻塞**替换成一个关于
                # 占位 commit 的血缘错误 —— 既掩盖真因，也让"待归档"看起来像"绑定错了"。
                # 2026-09-21 实测回归：backend/tests/contract/test_release_blockers_contract.py
                # 的 test_pending_claims_are_named_as_blockers 抓到了这次顺序错误。
                # 那条契约测试就是这条顺序的回归防线，不要为通过而改它。
                if repo_root is not None and claim.get("state") == "Verified":
                    lineage, lineage_error = commit_lineage(
                        repo_root,
                        (claim.get("target") or {}).get("artifact_commit"),
                        expected_commit,
                        allowed_prefixes,
                    )
                    if lineage_error is not None:
                        raise ValidationError(lineage_error["code"], lineage_error["message"])
                validate_verified_claim(
                    claim,
                    policy,
                    now_utc,
                    expected_commit,
                    expected_artifact_sha256,
                    artifact_commit_lineage=lineage,
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
