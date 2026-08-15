"""Behavior tests for fail-closed release-candidate provenance verification."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.release_governance.verify_candidate_provenance import (
    ARTIFACT_SHA256_MISMATCH,
    EXPECTED_COMMIT_MISMATCH,
    HEAD_COMMIT_MISMATCH,
    PROVENANCE_SCHEMA_INVALID,
    SCHEMA,
    main,
)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def candidate_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "release-governance@example.invalid")
    _git(repo, "config", "user.name", "Release Governance")
    (repo / "tracked.txt").write_text("candidate source\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _provenance(candidate, commit, **overrides):
    document = {
        "schema": SCHEMA,
        "git_commit": commit,
        "artifact_sha256": _sha256(candidate),
    }
    document.update(overrides)
    return document


def _write_provenance(repo, document):
    path = repo / "candidate.provenance.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _run(repo, candidate, provenance, expected_commit):
    return main(
        [
            "verify",
            "--candidate-path",
            str(candidate),
            "--provenance-json",
            str(provenance),
            "--expected-commit",
            expected_commit,
            "--repo-root",
            str(repo),
        ]
    )


def test_verify_accepts_candidate_bound_to_expected_commit_and_current_head(candidate_repo, capsys):
    candidate = candidate_repo / "release-candidate.tar.gz"
    candidate.write_bytes(b"immutable candidate bytes")
    commit = _head(candidate_repo)
    provenance = _write_provenance(candidate_repo, _provenance(candidate, commit))

    assert _run(candidate_repo, candidate, provenance, commit) == 0
    assert json.loads(capsys.readouterr().out) == {
        "artifact_sha256": _sha256(candidate),
        "git_commit": commit,
        "verdict": "pass",
    }


@pytest.mark.parametrize(
    ("document", "expected_commit", "error_code"),
    [
        ({}, "placeholder", PROVENANCE_SCHEMA_INVALID),
        ({"schema": "wrong"}, "placeholder", PROVENANCE_SCHEMA_INVALID),
        (
            {"schema": SCHEMA, "git_commit": "a" * 40, "artifact_sha256": "sha256:" + "0" * 64},
            "b" * 40,
            EXPECTED_COMMIT_MISMATCH,
        ),
    ],
)
def test_verify_rejects_invalid_provenance_schema_and_fields(candidate_repo, capsys, document, expected_commit, error_code):
    candidate = candidate_repo / "release-candidate.tar.gz"
    candidate.write_bytes(b"immutable candidate bytes")
    provenance = _write_provenance(candidate_repo, document)

    assert _run(candidate_repo, candidate, provenance, expected_commit) == 1
    assert capsys.readouterr().err.startswith(error_code + ":")


def test_verify_rejects_expected_commit_not_matching_provenance(candidate_repo, capsys):
    candidate = candidate_repo / "release-candidate.tar.gz"
    candidate.write_bytes(b"immutable candidate bytes")
    commit = _head(candidate_repo)
    provenance = _write_provenance(candidate_repo, _provenance(candidate, commit))

    assert _run(candidate_repo, candidate, provenance, "f" * 40) == 1
    assert capsys.readouterr().err.startswith(EXPECTED_COMMIT_MISMATCH + ":")


def test_verify_rejects_head_not_matching_provenance(candidate_repo, capsys):
    candidate = candidate_repo / "release-candidate.tar.gz"
    candidate.write_bytes(b"immutable candidate bytes")
    commit = _head(candidate_repo)
    provenance = _write_provenance(candidate_repo, _provenance(candidate, commit))
    (candidate_repo / "tracked.txt").write_text("advanced head\n", encoding="utf-8")
    _git(candidate_repo, "add", "tracked.txt")
    _git(candidate_repo, "commit", "-m", "advance head")

    assert _run(candidate_repo, candidate, provenance, commit) == 1
    assert capsys.readouterr().err.startswith(HEAD_COMMIT_MISMATCH + ":")


def test_verify_rejects_candidate_byte_hash_not_matching_provenance(candidate_repo, capsys):
    candidate = candidate_repo / "release-candidate.tar.gz"
    candidate.write_bytes(b"immutable candidate bytes")
    commit = _head(candidate_repo)
    provenance = _write_provenance(candidate_repo, _provenance(candidate, commit))
    candidate.write_bytes(b"tampered candidate bytes")

    assert _run(candidate_repo, candidate, provenance, commit) == 1
    assert capsys.readouterr().err.startswith(ARTIFACT_SHA256_MISMATCH + ":")
