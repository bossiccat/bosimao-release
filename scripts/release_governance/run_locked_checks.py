"""Run allow-listed release checks and persist non-overwritable command evidence."""

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


RESULT_SCHEMA = "release-governance/locked-check-result/v1"
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHECK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LockedCheckError(Exception):
    """A fail-closed runner error with a stable machine-readable code."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_release_id(release_id):
    if not isinstance(release_id, str) or not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise LockedCheckError(
            "INVALID_RELEASE_ID",
            "release_id must contain only letters, digits, dot, underscore, or hyphen",
        )


def _safe_check_id(check_id):
    if not isinstance(check_id, str) or not CHECK_ID_PATTERN.fullmatch(check_id):
        raise LockedCheckError(
            "INVALID_CHECK_ID",
            "check_id must contain only letters, digits, dot, underscore, or hyphen",
        )


def _load_check(command_lock_path, check_id):
    try:
        lock = json.loads(Path(command_lock_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockedCheckError("INVALID_COMMAND_LOCK", "cannot load command lock: %s" % exc) from exc

    checks = lock.get("checks")
    if not isinstance(checks, list):
        raise LockedCheckError("INVALID_COMMAND_LOCK", "checks must be a list")
    matches = [check for check in checks if isinstance(check, dict) and check.get("id") == check_id]
    if len(matches) != 1:
        raise LockedCheckError("CHECK_NOT_LOCKED", "check_id is not uniquely registered: %s" % check_id)

    check = matches[0]
    required = ("id", "cwd", "argv", "timeout_seconds", "expected_exit", "evidence_class")
    if any(field not in check for field in required):
        raise LockedCheckError("INVALID_LOCKED_CHECK", "locked check is missing required fields")
    if (
        not isinstance(check["cwd"], str)
        or not isinstance(check["argv"], list)
        or not check["argv"]
        or not all(isinstance(arg, str) and arg for arg in check["argv"])
        or not isinstance(check["timeout_seconds"], int)
        or check["timeout_seconds"] <= 0
        or not isinstance(check["expected_exit"], int)
        or check["evidence_class"] != "ci-command"
    ):
        raise LockedCheckError("INVALID_LOCKED_CHECK", "locked check fields have invalid values")
    return check


def get_current_commit(repo_root):
    """Return the current Git commit, rejecting an unavailable or ambiguous checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LockedCheckError("GIT_COMMIT_UNAVAILABLE", "cannot determine Git commit: %s" % exc) from exc
    commit = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise LockedCheckError("GIT_COMMIT_UNAVAILABLE", "cannot determine an immutable Git commit")
    return commit.lower()


def _environment_fingerprint():
    """Hash non-secret runtime metadata rather than serializing environment variables."""
    data = {
        "os_name": os.name,
        "platform": platform.platform(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "runner_executable": str(Path(sys.executable).resolve()),
    }
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


def _reserve_result_path(evidence_root, release_id, check_id):
    root = Path(evidence_root).resolve()
    target = root / release_id / "ci-command" / check_id
    if not _is_within(target.resolve(), root):
        raise LockedCheckError("EVIDENCE_PATH_ESCAPE", "evidence output escapes its root")
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise LockedCheckError(
            "EVIDENCE_ALREADY_EXISTS",
            "evidence already exists for release/check: %s/%s" % (release_id, check_id),
        ) from exc
    return target / "result.json"


def _write_result(result_path, result):
    payload = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with result_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise LockedCheckError("EVIDENCE_ALREADY_EXISTS", "result evidence already exists") from exc
    except OSError as exc:
        raise LockedCheckError("EVIDENCE_WRITE_FAILED", "cannot persist result evidence: %s" % exc) from exc


def run_locked_check(
    *,
    command_lock_path,
    check_id,
    release_id,
    repo_root,
    evidence_root,
    collector,
    requested_argv=None,
):
    """Execute exactly one command-lock entry and emit one immutable result.json."""
    if requested_argv is not None:
        raise LockedCheckError(
            "ARGUMENT_OVERRIDE_FORBIDDEN",
            "callers cannot append or replace locked argv",
        )
    _safe_release_id(release_id)
    _safe_check_id(check_id)
    if not isinstance(collector, str) or not collector:
        raise LockedCheckError("INVALID_COLLECTOR", "collector must be a non-empty string")

    repo = Path(repo_root).resolve()
    if not repo.is_dir():
        raise LockedCheckError("REPOSITORY_MISSING", "repository root does not exist")
    check = _load_check(command_lock_path, check_id)
    check_cwd = (repo / check["cwd"]).resolve()
    if not _is_within(check_cwd, repo):
        raise LockedCheckError("CWD_OUTSIDE_REPOSITORY", "locked cwd escapes repository root")
    if not check_cwd.is_dir():
        raise LockedCheckError("CWD_MISSING", "locked cwd does not exist")

    commit = get_current_commit(repo)
    result_path = _reserve_result_path(evidence_root, release_id, check_id)
    argv = list(check["argv"])
    started_at = _utc_now()
    base_result = {
        "schema": RESULT_SCHEMA,
        "release_id": release_id,
        "check_id": check_id,
        "git_commit": commit,
        "argv": argv,
        "cwd": str(check_cwd),
        "started_at": started_at,
        "expected_exit": check["expected_exit"],
        "environment_fingerprint": _environment_fingerprint(),
        "collector": collector,
    }

    try:
        completed = subprocess.run(
            argv,
            cwd=str(check_cwd),
            check=False,
            shell=False,
            capture_output=True,
            timeout=check["timeout_seconds"],
        )
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        result = {
            **base_result,
            "finished_at": _utc_now(),
            "exit_code": completed.returncode,
            "stdout_sha256": _sha256_bytes(stdout),
            "stderr_sha256": _sha256_bytes(stderr),
            "status": "passed" if completed.returncode == check["expected_exit"] else "failed",
        }
        _write_result(result_path, result)
        if completed.returncode != check["expected_exit"]:
            raise LockedCheckError(
                "CHECK_EXIT_MISMATCH",
                "locked check exited %s; expected %s" % (completed.returncode, check["expected_exit"]),
            )
        return result
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        _write_result(
            result_path,
            {
                **base_result,
                "finished_at": _utc_now(),
                "exit_code": None,
                "stdout_sha256": _sha256_bytes(stdout),
                "stderr_sha256": _sha256_bytes(stderr),
                "status": "timeout",
            },
        )
        raise LockedCheckError(
            "CHECK_TIMEOUT",
            "locked check exceeded %s seconds" % check["timeout_seconds"],
        ) from exc
    except OSError as exc:
        encoded = str(exc).encode("utf-8", errors="replace")
        _write_result(
            result_path,
            {
                **base_result,
                "finished_at": _utc_now(),
                "exit_code": None,
                "stdout_sha256": _sha256_bytes(b""),
                "stderr_sha256": _sha256_bytes(encoded),
                "status": "launch-error",
            },
        )
        raise LockedCheckError("CHECK_LAUNCH_FAILED", "cannot start locked check: %s" % exc) from exc
