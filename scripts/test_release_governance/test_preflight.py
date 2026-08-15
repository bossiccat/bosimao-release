"""RED integration tests for the fail-closed release preflight CLI (Task 4)."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "scripts" / "release-preflight.py"


def _sha256_file(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command, cwd):
    return subprocess.run(command, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git(repo, *args):
    return _run(["git", *args], repo).stdout.strip()


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    (repo / "source.txt").write_text("tracked source\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _claim(claim_id, commit, artifact_sha, evidence_path, state="Verified"):
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    return {
        "claim_id": claim_id,
        "state": state,
        "owner": "implementation",
        "reviewer": "independent-reviewer",
        "target": {"artifact_commit": commit, "artifact_sha256": artifact_sha},
        "evidence": [
            {
                "kind": "windows-field" if claim_id == "windows-popup-free" else "android-field",
                "collected_at": "2026-08-15T00:00:00Z",
                "expires_at": expiry.isoformat().replace("+00:00", "Z"),
                "path": str(evidence_path),
                "raw_sha256": _sha256_file(evidence_path),
            }
        ],
        "attempts": [],
    }


def _fixture(tmp_path, state="Verified", checks=None):
    repo, commit = _init_repo(tmp_path)
    inputs = tmp_path / "release-inputs"
    artifact = inputs / "candidate.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"candidate artifact bytes")
    artifact_sha = _sha256_file(artifact)
    evidence = inputs / "field-evidence.log"
    evidence.write_text("field evidence\n", encoding="utf-8")
    claims = inputs / "claims"
    _write_json(claims / "windows-popup-free.json", _claim("windows-popup-free", commit, artifact_sha, evidence, state))
    _write_json(claims / "android-duplex-audio.json", _claim("android-duplex-audio", commit, artifact_sha, evidence, state))
    policy = inputs / "policy.json"
    _write_json(
        policy,
        {
            "schema_version": 1,
            "required_claim_ids": ["windows-popup-free", "android-duplex-audio"],
            "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
            "max_attempts_same_fingerprint": 2,
            "max_evidence_age_hours": 72,
            "required_checks": [check["id"] for check in (checks or [])],
            "release_channel": "production",
        },
    )
    lock = inputs / "command-lock.json"
    _write_json(lock, {"schema_version": 1, "checks": checks or []})
    return {
        "repo": repo,
        "commit": commit,
        "artifact": artifact,
        "artifact_sha": artifact_sha,
        "claims": claims,
        "policy": policy,
        "lock": lock,
        "evidence_root": tmp_path / "release-evidence",
    }


def _check(check_id, source, expected_exit=0, timeout_seconds=5):
    return {
        "id": check_id,
        "cwd": ".",
        "argv": [sys.executable, "-c", source],
        "timeout_seconds": timeout_seconds,
        "expected_exit": expected_exit,
        "evidence_class": "ci-command",
    }


def _preflight(fixture, action, release_id=None, ci_run_url="https://ci.example.invalid/runs/1", env=None):
    command = [
        sys.executable,
        str(PREFLIGHT),
        action,
        "--policy",
        str(fixture["policy"]),
        "--claims",
        str(fixture["claims"]),
        "--command-lock",
        str(fixture["lock"]),
        "--repo-root",
        str(fixture["repo"]),
        "--artifact-path",
        str(fixture["artifact"]),
        "--evidence-root",
        str(fixture["evidence_root"]),
    ]
    if action == "release":
        command += ["--release-id", release_id, "--ci-run-url", ci_run_url]
    process_env = os.environ.copy()
    process_env.setdefault("RELEASE_EVIDENCE_HMAC_KEY", "release-test-key")
    if env:
        process_env.update(env)
    return subprocess.run(command, cwd=str(REPO_ROOT), capture_output=True, text=True, env=process_env)


def test_verify_only_validates_and_writes_no_release_evidence(tmp_path):
    fixture = _fixture(tmp_path, state="EvidencePending")

    result = _preflight(fixture, "verify")

    assert result.returncode != 0
    assert "NOT_VERIFIED" in result.stdout
    assert not fixture["evidence_root"].exists()


def test_release_rejects_evidence_pending_claim_without_creating_manifest(tmp_path):
    fixture = _fixture(tmp_path, state="EvidencePending")

    result = _preflight(fixture, "release", release_id="pending-001")

    assert result.returncode != 0
    assert "NOT_VERIFIED" in result.stdout
    assert not (fixture["evidence_root"] / "pending-001").exists()


def test_release_stops_at_failing_locked_check_without_manifest(tmp_path):
    fixture = _fixture(
        tmp_path,
        checks=[
            _check("fails", "raise SystemExit(4)"),
            _check("must-not-run", "raise SystemExit(8)"),
        ],
    )

    result = _preflight(fixture, "release", release_id="failed-check-001")

    assert result.returncode != 0
    release_dir = fixture["evidence_root"] / "failed-check-001"
    assert (release_dir / "ci-command" / "fails" / "result.json").is_file()
    assert not (release_dir / "ci-command" / "must-not-run").exists()
    assert not (release_dir / "release-manifest.json").exists()


def test_release_creates_manifest_only_after_verified_claims_and_checks_pass(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])

    result = _preflight(fixture, "release", release_id="pass-001")

    assert result.returncode == 0, result.stderr
    manifest_path = fixture["evidence_root"] / "pass-001" / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["git_commit"] == fixture["commit"]
    assert manifest["artifact_sha256"] == fixture["artifact_sha"]
    assert manifest["ci_run_url"] == "https://ci.example.invalid/runs/1"
    assert manifest["required_checks"][0]["check_id"] == "pass"
    assert manifest["required_checks"][0]["result_sha256"].startswith("sha256:")
    assert manifest["policy_sha256"] == _sha256_file(fixture["policy"])
    assert manifest["created_at"].endswith("Z")


def test_release_rejects_duplicate_release_id_without_overwriting_manifest(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])
    first = _preflight(fixture, "release", release_id="immutable-001")
    manifest_path = fixture["evidence_root"] / "immutable-001" / "release-manifest.json"
    before = manifest_path.read_bytes()

    second = _preflight(fixture, "release", release_id="immutable-001")

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert manifest_path.read_bytes() == before


def test_release_rejects_preexisting_release_reservation_without_running_checks(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])
    reserved = fixture["evidence_root"] / "reserved-001"
    reserved.mkdir(parents=True)

    result = _preflight(fixture, "release", release_id="reserved-001")

    assert result.returncode != 0
    assert "RELEASE_ID_ALREADY_USED" in result.stdout
    assert not (reserved / "ci-command").exists()


def test_release_rejects_duplicate_required_check_before_reserving_evidence(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["pass", "pass"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="duplicate-check-001")

    assert result.returncode != 0
    assert "DUPLICATE_REQUIRED_CHECK" in result.stdout
    assert not (fixture["evidence_root"] / "duplicate-check-001").exists()


def test_release_rejects_tampered_locked_result_hmac_without_manifest(tmp_path):
    fixture = _fixture(tmp_path)
    first = _check("first", "print('first')")
    first_result = fixture["evidence_root"] / "tampered-result-001" / "ci-command" / "first" / "result.json"
    second = _check(
        "tamper",
        "from pathlib import Path; Path(r'%s').write_text('{}', encoding='utf-8')" % first_result,
    )
    lock = json.loads(fixture["lock"].read_text(encoding="utf-8"))
    lock["checks"] = [first, second]
    _write_json(fixture["lock"], lock)
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["first", "tamper"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="tampered-result-001")

    assert result.returncode != 0
    assert "RESULT_HMAC_MISMATCH" in result.stdout
    assert not (fixture["evidence_root"] / "tampered-result-001" / "release-manifest.json").exists()


def test_release_rechecks_artifact_after_claim_evidence_before_manifest(tmp_path):
    fixture = _fixture(tmp_path)
    check = _check(
        "mutates-artifact",
        "from pathlib import Path; Path(r'%s').write_bytes(b'changed')" % fixture["artifact"],
    )
    lock = json.loads(fixture["lock"].read_text(encoding="utf-8"))
    lock["checks"] = [check]
    _write_json(fixture["lock"], lock)
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["mutates-artifact"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="mutated-artifact-001")

    assert result.returncode != 0
    assert "RELEASE_INPUT_CHANGED" in result.stdout
    assert not (fixture["evidence_root"] / "mutated-artifact-001" / "release-manifest.json").exists()


def test_release_rejects_initial_dirty_worktree_before_reserving_evidence(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])
    (fixture["repo"] / "untracked.txt").write_text("must block release\n", encoding="utf-8")

    result = _preflight(fixture, "release", release_id="initial-dirty-001")

    assert result.returncode != 0
    assert "DIRTY_WORKTREE" in result.stdout
    assert not (fixture["evidence_root"] / "initial-dirty-001").exists()


def test_release_rejects_missing_required_check_before_reserving_evidence(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["missing"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="missing-check-001")

    assert result.returncode != 0
    assert "REQUIRED_CHECK_NOT_LOCKED" in result.stdout
    assert not (fixture["evidence_root"] / "missing-check-001").exists()


def test_release_rejects_non_https_ci_url_before_reserving_evidence(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])

    result = _preflight(
        fixture,
        "release",
        release_id="invalid-url-001",
        ci_run_url="http://ci.example.invalid/runs/1",
    )

    assert result.returncode != 0
    assert "INVALID_CI_RUN_URL" in result.stdout
    assert not (fixture["evidence_root"] / "invalid-url-001").exists()


def test_release_rejects_missing_evidence_hmac_key_fail_closed(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])

    result = _preflight(
        fixture,
        "release",
        release_id="missing-hmac-key-001",
        env={"RELEASE_EVIDENCE_HMAC_KEY": ""},
    )

    assert result.returncode != 0
    assert "EVIDENCE_HMAC_KEY_MISSING" in result.stdout
    assert not (fixture["evidence_root"] / "missing-hmac-key-001" / "release-manifest.json").exists()


def test_release_returns_structured_failure_for_non_object_locked_result(tmp_path):
    fixture = _fixture(tmp_path)
    first = _check("first", "print('first')")
    result_path = fixture["evidence_root"] / "non-object-result-001" / "ci-command" / "first" / "result.json"
    tamper = _check(
        "tamper",
        "import hashlib, hmac, json; from pathlib import Path; p=Path(r'%s'); raw=b'[]\\n'; p.write_bytes(raw); p.with_suffix('.hmac').write_text(hmac.new(b'release-test-key', raw, hashlib.sha256).hexdigest(), encoding='ascii')" % result_path,
    )
    lock = json.loads(fixture["lock"].read_text(encoding="utf-8"))
    lock["checks"] = [first, tamper]
    _write_json(fixture["lock"], lock)
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["first", "tamper"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="non-object-result-001")

    assert result.returncode != 0
    assert result.stdout.strip().startswith("{")
    assert "INVALID_CHECK_RESULT" in result.stdout or "REQUIRED_CHECK_RESULTS_INVALID" in result.stdout
    assert not (fixture["evidence_root"] / "non-object-result-001" / "release-manifest.json").exists()


def test_release_rejects_required_path_like_check_before_reserving_evidence(tmp_path):
    fixture = _fixture(tmp_path, checks=[_check("pass", "print('pass')")])
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["../escape"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="unsafe-required-001")

    assert result.returncode != 0
    assert "INVALID_REQUIRED_CHECK_ID" in result.stdout
    assert not (fixture["evidence_root"] / "unsafe-required-001").exists()


def test_release_rejects_check_that_dirties_worktree_before_later_check_can_restore_it(tmp_path):
    fixture = _fixture(tmp_path)
    mutates = _check(
        "mutates-worktree",
        "from pathlib import Path; Path('source.txt').write_text('temporary mutation\\n', encoding='utf-8')",
    )
    restore_marker = fixture["repo"] / "restore-ran.txt"
    restores = _check(
        "must-not-restore",
        "from pathlib import Path; Path('source.txt').write_text('tracked source\\n', encoding='utf-8'); Path(r'%s').write_text('ran', encoding='utf-8')" % restore_marker,
    )
    lock = json.loads(fixture["lock"].read_text(encoding="utf-8"))
    lock["checks"] = [mutates, restores]
    _write_json(fixture["lock"], lock)
    policy = json.loads(fixture["policy"].read_text(encoding="utf-8"))
    policy["required_checks"] = ["mutates-worktree", "must-not-restore"]
    _write_json(fixture["policy"], policy)

    result = _preflight(fixture, "release", release_id="intermediate-dirty-001")

    assert result.returncode != 0
    assert "DIRTY_WORKTREE" in result.stdout
    assert not restore_marker.exists()
    assert not (fixture["evidence_root"] / "intermediate-dirty-001" / "release-manifest.json").exists()
