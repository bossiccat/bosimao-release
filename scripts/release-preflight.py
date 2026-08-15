"""The sole local CLI for fail-closed release preflight and manifest creation."""

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_governance.run_locked_checks import (  # noqa: E402
    LockedCheckError,
    get_current_commit,
    run_locked_check,
)
from scripts.release_governance.verify import detect_worktree_clean, verify_claims  # noqa: E402


RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MANIFEST_SCHEMA = "release-governance/release-manifest/v1"


class PreflightError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _fsync_file(path):
    try:
        descriptor = os.open(str(path), os.O_RDWR)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PreflightError("FILE_FSYNC_FAILED", "cannot persist file: %s" % exc) from exc


def _fsync_directory(path):
    if os.name == "nt":
        # Windows has no portable directory-fsync primitive. The manifest's
        # same-volume MoveFileExW(..., MOVEFILE_WRITE_THROUGH) is the durable
        # commit barrier; callers must use _commit_manifest_windows().
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PreflightError("DIRECTORY_FSYNC_FAILED", "cannot persist directory entry: %s" % exc) from exc


def _commit_manifest_windows(staging_path, manifest_path):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    movefile_write_through = 0x00000008
    if not kernel32.MoveFileExW(str(staging_path), str(manifest_path), movefile_write_through):
        error = ctypes.get_last_error()
        if error in (183,):
            raise PreflightError("MANIFEST_ALREADY_EXISTS", "release manifest already exists")
        raise PreflightError("MANIFEST_COMMIT_FAILED", "MoveFileExW failed with error %s" % error)


def _load_json(path, code):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(code, "cannot load JSON: %s" % exc) from exc


def _validate_release_id(release_id):
    if not isinstance(release_id, str) or not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise PreflightError("INVALID_RELEASE_ID", "release_id has an invalid format")


def _validate_ci_run_url(ci_run_url):
    parsed = urlparse(ci_run_url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise PreflightError("INVALID_CI_RUN_URL", "ci_run_url must be an absolute HTTPS URL")


def _snapshot(repo_root, artifact_path):
    repo = Path(repo_root).resolve()
    artifact = Path(artifact_path).resolve()
    if not repo.is_dir():
        raise PreflightError("REPOSITORY_MISSING", "repo_root does not exist")
    if not artifact.is_file():
        raise PreflightError("ARTIFACT_MISSING", "artifact_path does not exist or is not a file")
    if not detect_worktree_clean(repo):
        raise PreflightError("DIRTY_WORKTREE", "release requires a clean worktree")
    return {"git_commit": get_current_commit(repo), "artifact_sha256": _sha256_file(artifact)}


def _verify(policy_path, claims_path, repo_root, artifact_path):
    policy = _load_json(policy_path, "INVALID_POLICY")
    snapshot = _snapshot(repo_root, artifact_path)
    result = verify_claims(
        policy,
        claims_path,
        expected_commit=snapshot["git_commit"],
        expected_artifact_sha256=snapshot["artifact_sha256"],
        now_utc=datetime.now(timezone.utc),
        worktree_clean=True,
    )
    return policy, snapshot, result


def _release_dir(evidence_root, release_id):
    root = Path(evidence_root).resolve()
    target = root / release_id
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PreflightError("EVIDENCE_PATH_ESCAPE", "release evidence path escapes its root") from exc
    return target


def _reserve_new_release(evidence_root, release_id):
    release_dir = _release_dir(evidence_root, release_id)
    try:
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        release_dir.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise PreflightError("RELEASE_ID_ALREADY_USED", "release_id already has reserved evidence") from exc
    except OSError as exc:
        raise PreflightError("RELEASE_RESERVATION_FAILED", "cannot reserve release id: %s" % exc) from exc
    return release_dir


def _validate_required_checks(policy, command_lock):
    required = policy.get("required_checks")
    if not isinstance(required, list) or not all(isinstance(check_id, str) and check_id for check_id in required):
        raise PreflightError("INVALID_REQUIRED_CHECKS", "required_checks must be a list of non-empty strings")
    if len(required) != len(set(required)):
        raise PreflightError("DUPLICATE_REQUIRED_CHECK", "required_checks cannot contain duplicates")
    invalid = [check_id for check_id in required if not RELEASE_ID_PATTERN.fullmatch(check_id)]
    if invalid:
        raise PreflightError("INVALID_REQUIRED_CHECK_ID", "required check ids have an invalid format")
    checks = command_lock.get("checks")
    if not isinstance(checks, list):
        raise PreflightError("INVALID_COMMAND_LOCK", "checks must be a list")
    locked_ids = [entry.get("id") for entry in checks if isinstance(entry, dict)]
    if len(locked_ids) != len(set(locked_ids)):
        raise PreflightError("DUPLICATE_LOCKED_CHECK", "command lock cannot contain duplicate check ids")
    unknown = [check_id for check_id in required if check_id not in locked_ids]
    if unknown:
        raise PreflightError("REQUIRED_CHECK_NOT_LOCKED", "required checks not locked: %s" % ", ".join(unknown))


def _load_required_results(policy, command_lock, evidence_root, release_id, commit):
    key_value = os.environ.get("RELEASE_EVIDENCE_HMAC_KEY")
    if not key_value:
        raise PreflightError("EVIDENCE_HMAC_KEY_MISSING", "CI evidence HMAC key is required")
    hmac_key = key_value.encode("utf-8")
    lock_by_id = {entry.get("id"): entry for entry in command_lock.get("checks", []) if isinstance(entry, dict)}
    errors = []
    results = []
    for check_id in policy.get("required_checks", []):
        check = lock_by_id.get(check_id)
        if not check:
            errors.append({"code": "REQUIRED_CHECK_NOT_LOCKED", "check_id": check_id})
            continue
        result_path = _release_dir(evidence_root, release_id) / "ci-command" / check_id / "result.json"
        sidecar_path = result_path.with_suffix(".hmac")
        try:
            raw_result = result_path.read_bytes()
            provided_hmac = sidecar_path.read_text(encoding="ascii").strip()
            expected_hmac = hmac.new(hmac_key, raw_result, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(provided_hmac, expected_hmac):
                errors.append({"code": "RESULT_HMAC_MISMATCH", "check_id": check_id})
                continue
            result = json.loads(raw_result.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("locked check result must be a JSON object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append({"code": "INVALID_CHECK_RESULT", "check_id": check_id, "message": str(exc)})
            continue
        if result.get("schema") != "release-governance/locked-check-result/v1":
            errors.append({"code": "RESULT_SCHEMA_MISMATCH", "check_id": check_id})
        if result.get("release_id") != release_id:
            errors.append({"code": "RESULT_RELEASE_ID_MISMATCH", "check_id": check_id})
        if result.get("check_id") != check_id:
            errors.append({"code": "RESULT_CHECK_ID_MISMATCH", "check_id": check_id})
        if result.get("git_commit") != commit:
            errors.append({"code": "RESULT_COMMIT_MISMATCH", "check_id": check_id})
        if result.get("status") != "passed":
            errors.append({"code": "RESULT_NOT_PASSED", "check_id": check_id})
        if result.get("exit_code") != check.get("expected_exit"):
            errors.append({"code": "RESULT_EXIT_MISMATCH", "check_id": check_id})
        results.append({"check_id": check_id, "result_sha256": _sha256_file(result_path)})
    if errors:
        raise PreflightError("REQUIRED_CHECK_RESULTS_INVALID", json.dumps(errors, sort_keys=True))
    return results


def _claim_evidence_digests(claims_path):
    digests = []
    for claim_path in sorted(Path(claims_path).glob("*.json")):
        claim = _load_json(claim_path, "INVALID_CLAIM_JSON")
        for evidence in claim.get("evidence") or []:
            digest = evidence.get("raw_sha256") or evidence.get("result_sha256")
            if not digest:
                raise PreflightError("EVIDENCE_DIGEST_MISSING", "claim evidence digest is missing")
            digests.append({"claim_id": claim.get("claim_id"), "evidence_sha256": digest})
    return digests


def _write_manifest(release_dir, manifest):
    manifest_path = release_dir / "release-manifest.json"
    if manifest_path.exists():
        raise PreflightError("MANIFEST_ALREADY_EXISTS", "release manifest already exists")
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    release_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor, raw_staging = tempfile.mkstemp(prefix=".manifest-staging-", suffix=".json", dir=str(release_dir))
        staging_path = Path(raw_staging)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _commit_manifest_windows(staging_path, manifest_path)
        else:
            try:
                os.link(staging_path, manifest_path)
            except FileExistsError as exc:
                raise PreflightError("MANIFEST_ALREADY_EXISTS", "release manifest already exists") from exc
        _fsync_file(manifest_path)
        _fsync_directory(release_dir)
        _fsync_directory(release_dir.parent)
    except OSError as exc:
        raise PreflightError("MANIFEST_WRITE_FAILED", "cannot atomically write manifest: %s" % exc) from exc
    return manifest_path


def _release(args):
    _validate_release_id(args.release_id)
    _validate_ci_run_url(args.ci_run_url)
    policy, initial, verification = _verify(args.policy, args.claims, args.repo_root, args.artifact_path)
    if verification["verdict"] != "pass":
        return {"verdict": "fail", "errors": verification["errors"]}
    command_lock = _load_json(args.command_lock, "INVALID_COMMAND_LOCK")
    _validate_required_checks(policy, command_lock)
    release_dir = _reserve_new_release(args.evidence_root, args.release_id)
    for check_id in policy.get("required_checks", []):
        try:
            run_locked_check(
                command_lock_path=args.command_lock,
                check_id=check_id,
                release_id=args.release_id,
                repo_root=args.repo_root,
                evidence_root=args.evidence_root,
                collector=args.collector,
            )
        except LockedCheckError as exc:
            return {"verdict": "fail", "errors": [{"code": exc.code, "message": exc.message}]}
        current = _snapshot(args.repo_root, args.artifact_path)
        if current != initial:
            raise PreflightError(
                "RELEASE_INPUT_CHANGED",
                "HEAD, worktree, or artifact bytes changed during locked checks",
            )

    results = _load_required_results(
        policy, command_lock, args.evidence_root, args.release_id, initial["git_commit"]
    )
    final = _snapshot(args.repo_root, args.artifact_path)
    if final != initial:
        raise PreflightError("RELEASE_INPUT_CHANGED", "HEAD or artifact bytes changed during preflight")

    committed_inputs = _snapshot(args.repo_root, args.artifact_path)
    if committed_inputs != initial:
        raise PreflightError("RELEASE_INPUT_CHANGED", "HEAD or artifact bytes changed before manifest commit")

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "release_id": args.release_id,
        "policy_sha256": _sha256_file(args.policy),
        "git_commit": initial["git_commit"],
        "artifact_sha256": initial["artifact_sha256"],
        "claim_evidence": _claim_evidence_digests(args.claims),
        "required_checks": results,
        "created_at": _utc_now(),
        "ci_run_url": args.ci_run_url,
    }
    manifest_path = _write_manifest(release_dir, manifest)
    return {"verdict": "pass", "manifest": str(manifest_path)}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("verify", "release"):
        child = subparsers.add_parser(action)
        child.add_argument("--policy", required=True)
        child.add_argument("--claims", required=True)
        child.add_argument("--command-lock", required=True)
        child.add_argument("--repo-root", required=True)
        child.add_argument("--artifact-path", required=True)
        child.add_argument("--evidence-root", required=True)
        if action == "release":
            child.add_argument("--release-id", required=True)
            child.add_argument("--ci-run-url", required=True)
            child.add_argument("--collector", default="release-governance-ci")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.action == "verify":
            _, _, result = _verify(args.policy, args.claims, args.repo_root, args.artifact_path)
        else:
            result = _release(args)
    except (PreflightError, LockedCheckError) as exc:
        result = {"verdict": "fail", "errors": [{"code": exc.code, "message": exc.message}]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
