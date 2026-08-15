"""RED tests for the immutable locked release-check runner (Task 3)."""

import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.release_governance.run_locked_checks import (
    LockedCheckError,
    run_locked_check,
)


COMMIT = "abc123def456"
COLLECTOR = "release-governance-ci"


def _sha256_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_lock(path, checks):
    path.write_text(
        json.dumps({"schema_version": 1, "checks": checks}), encoding="utf-8"
    )


def _check(check_id, argv, **overrides):
    check = {
        "id": check_id,
        "cwd": ".",
        "argv": argv,
        "timeout_seconds": 5,
        "expected_exit": 0,
        "evidence_class": "ci-command",
    }
    check.update(overrides)
    return check


def _runner(tmp_path, monkeypatch, checks):
    lock_path = tmp_path / "command-lock.json"
    evidence_root = tmp_path / "evidence"
    _write_lock(lock_path, checks)
    monkeypatch.setenv("RELEASE_EVIDENCE_HMAC_KEY", "release-test-key")
    monkeypatch.setattr(
        "scripts.release_governance.run_locked_checks.get_current_commit",
        lambda repo_root: COMMIT,
    )

    def call(check_id, release_id="release-001", **kwargs):
        return run_locked_check(
            command_lock_path=lock_path,
            check_id=check_id,
            release_id=release_id,
            repo_root=tmp_path,
            evidence_root=evidence_root,
            collector=COLLECTOR,
            **kwargs,
        )

    return call, evidence_root


def test_unregistered_check_is_rejected(tmp_path, monkeypatch):
    call, _ = _runner(tmp_path, monkeypatch, [])

    with pytest.raises(LockedCheckError, match="CHECK_NOT_LOCKED"):
        call("not-in-lock")


def test_caller_supplied_arguments_are_rejected(tmp_path, monkeypatch):
    call, _ = _runner(
        tmp_path,
        monkeypatch,
        [_check("fixture", [sys.executable, "-c", "print('locked')"])],
    )

    with pytest.raises(LockedCheckError, match="ARGUMENT_OVERRIDE_FORBIDDEN"):
        call("fixture", requested_argv=["--not-allowed"])


def test_cwd_escape_is_rejected_before_command_execution(tmp_path, monkeypatch):
    call, _ = _runner(
        tmp_path,
        monkeypatch,
        [_check("escaped", [sys.executable, "-c", "raise SystemExit(99)"], cwd="../outside")],
    )

    with pytest.raises(LockedCheckError, match="CWD_OUTSIDE_REPOSITORY"):
        call("escaped")


def test_path_like_check_id_is_rejected_before_evidence_output(tmp_path, monkeypatch):
    call, _ = _runner(
        tmp_path,
        monkeypatch,
        [_check("../escaped", [sys.executable, "-c", "raise SystemExit(99)"])],
    )

    with pytest.raises(LockedCheckError, match="INVALID_CHECK_ID"):
        call("../escaped")


def test_runner_uses_exact_locked_argv_without_shell(tmp_path, monkeypatch):
    locked_argv = [sys.executable, "-c", "print('locked')"]
    call, _ = _runner(tmp_path, monkeypatch, [_check("exact", locked_argv)])
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "scripts.release_governance.run_locked_checks.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "scripts.release_governance.run_locked_checks._environment_fingerprint",
        lambda: {"platform": "test", "python": "test"},
    )

    call("exact")

    assert observed["argv"] == locked_argv
    assert observed["shell"] is False


def test_timeout_is_recorded_and_rejected(tmp_path, monkeypatch):
    call, evidence_root = _runner(
        tmp_path,
        monkeypatch,
        [
            _check(
                "slow",
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=1,
            )
        ],
    )

    with pytest.raises(LockedCheckError, match="CHECK_TIMEOUT"):
        call("slow")

    result_path = evidence_root / "release-001" / "ci-command" / "slow" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "timeout"
    assert result["exit_code"] is None


def test_unexpected_exit_is_recorded_and_rejected(tmp_path, monkeypatch):
    call, evidence_root = _runner(
        tmp_path,
        monkeypatch,
        [_check("bad-exit", [sys.executable, "-c", "raise SystemExit(7)"])],
    )

    with pytest.raises(LockedCheckError, match="CHECK_EXIT_MISMATCH"):
        call("bad-exit")

    result_path = evidence_root / "release-001" / "ci-command" / "bad-exit" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["exit_code"] == 7


def test_result_records_hashes_utc_commit_and_environment(tmp_path, monkeypatch):
    line_ending = b"\r\n" if sys.platform == "win32" else b"\n"
    stdout = b"fixture stdout" + line_ending
    stderr = b"fixture stderr" + line_ending
    call, evidence_root = _runner(
        tmp_path,
        monkeypatch,
        [
            _check(
                "records",
                [
                    sys.executable,
                    "-c",
                    "import sys; print('fixture stdout'); print('fixture stderr', file=sys.stderr)",
                ],
            )
        ],
    )

    result = call("records")
    result_path = evidence_root / "release-001" / "ci-command" / "records" / "result.json"
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    sidecar_path = result_path.with_suffix(".hmac")
    assert sidecar_path.is_file()
    expected_hmac = hmac.new(b"release-test-key", result_path.read_bytes(), hashlib.sha256).hexdigest()
    assert sidecar_path.read_text(encoding="ascii") == expected_hmac

    assert result == persisted
    assert persisted["schema"] == "release-governance/locked-check-result/v1"
    assert persisted["release_id"] == "release-001"
    assert persisted["check_id"] == "records"
    assert persisted["git_commit"] == COMMIT
    assert persisted["argv"] == [
        sys.executable,
        "-c",
        "import sys; print('fixture stdout'); print('fixture stderr', file=sys.stderr)",
    ]
    assert persisted["started_at"].endswith("Z")
    assert persisted["finished_at"].endswith("Z")
    assert persisted["stdout_sha256"] == _sha256_bytes(stdout)
    assert persisted["stderr_sha256"] == _sha256_bytes(stderr)
    assert persisted["environment_fingerprint"].startswith("sha256:")
    assert persisted["collector"] == COLLECTOR
    assert persisted["status"] == "passed"


def test_existing_release_check_evidence_cannot_be_overwritten(tmp_path, monkeypatch):
    call, evidence_root = _runner(
        tmp_path,
        monkeypatch,
        [_check("immutable", [sys.executable, "-c", "print('first')"])],
    )

    first = call("immutable")
    result_path = evidence_root / "release-001" / "ci-command" / "immutable" / "result.json"

    with pytest.raises(LockedCheckError, match="EVIDENCE_ALREADY_EXISTS"):
        call("immutable")

    assert json.loads(result_path.read_text(encoding="utf-8")) == first


def test_locked_child_does_not_inherit_evidence_hmac_key(tmp_path, monkeypatch):
    call, _ = _runner(
        tmp_path,
        monkeypatch,
        [
            _check(
                "no-secret-leak",
                [
                    sys.executable,
                    "-c",
                    "import os, sys; raise SystemExit(37 if 'RELEASE_EVIDENCE_HMAC_KEY' in os.environ else 0)",
                ],
            )
        ],
    )

    result = call("no-secret-leak")

    assert result["status"] == "passed"
