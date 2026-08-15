"""Create and verify the identity binding for a release-candidate artifact."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "release-governance/candidate-provenance/v1"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

ARTIFACT_SHA256_MISMATCH = "ARTIFACT_SHA256_MISMATCH"
EXPECTED_COMMIT_INVALID = "EXPECTED_COMMIT_INVALID"
EXPECTED_COMMIT_MISMATCH = "EXPECTED_COMMIT_MISMATCH"
HEAD_COMMIT_MISMATCH = "HEAD_COMMIT_MISMATCH"
PROVENANCE_SCHEMA_INVALID = "PROVENANCE_SCHEMA_INVALID"


class ProvenanceError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _sha256_file(candidate_path):
    digest = hashlib.sha256()
    try:
        with Path(candidate_path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ProvenanceError("CANDIDATE_UNREADABLE", "cannot read candidate: %s" % exc) from exc
    return "sha256:" + digest.hexdigest()


def _current_head(repo_root):
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(repo_root).resolve()),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvenanceError("HEAD_UNAVAILABLE", "cannot resolve current HEAD: %s" % exc) from exc
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not COMMIT_PATTERN.fullmatch(commit):
        raise ProvenanceError("HEAD_UNAVAILABLE", "cannot resolve a full current HEAD commit")
    return commit


def _load_provenance(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(PROVENANCE_SCHEMA_INVALID, "cannot load provenance JSON: %s" % exc) from exc
    if not isinstance(document, dict):
        raise ProvenanceError(PROVENANCE_SCHEMA_INVALID, "provenance must be a JSON object")
    if set(document) != {"schema", "git_commit", "artifact_sha256"}:
        raise ProvenanceError(PROVENANCE_SCHEMA_INVALID, "provenance fields must be schema, git_commit, artifact_sha256")
    if document.get("schema") != SCHEMA:
        raise ProvenanceError(PROVENANCE_SCHEMA_INVALID, "unsupported provenance schema")
    if not COMMIT_PATTERN.fullmatch(document.get("git_commit", "")):
        raise ProvenanceError(PROVENANCE_SCHEMA_INVALID, "provenance git_commit must be a full lowercase SHA-1")
    if not SHA256_PATTERN.fullmatch(document.get("artifact_sha256", "")):
        raise ProvenanceError(PROVENANCE_SCHEMA_INVALID, "provenance artifact_sha256 must be a SHA-256 digest")
    return document


def create_provenance(candidate_path, repo_root):
    return {
        "schema": SCHEMA,
        "git_commit": _current_head(repo_root),
        "artifact_sha256": _sha256_file(candidate_path),
    }


def verify_provenance(candidate_path, provenance_path, expected_commit, repo_root):
    provenance = _load_provenance(provenance_path)
    if not COMMIT_PATTERN.fullmatch(expected_commit or ""):
        raise ProvenanceError(EXPECTED_COMMIT_INVALID, "expected_commit must be a full lowercase SHA-1")
    if provenance["git_commit"] != expected_commit:
        raise ProvenanceError(
            EXPECTED_COMMIT_MISMATCH,
            "provenance git_commit does not match expected_commit",
        )
    current_head = _current_head(repo_root)
    if expected_commit != current_head:
        raise ProvenanceError(
            HEAD_COMMIT_MISMATCH,
            "expected_commit does not match current repository HEAD",
        )
    actual_sha256 = _sha256_file(candidate_path)
    if provenance["artifact_sha256"] != actual_sha256:
        raise ProvenanceError(
            ARTIFACT_SHA256_MISMATCH,
            "candidate SHA-256 does not match provenance artifact_sha256",
        )
    return {"verdict": "pass", "git_commit": current_head, "artifact_sha256": actual_sha256}


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    create = command.add_parser("create", help="write provenance for a candidate")
    create.add_argument("--candidate-path", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--repo-root", required=True)
    verify = command.add_parser("verify", help="verify a candidate against provenance")
    verify.add_argument("--candidate-path", required=True)
    verify.add_argument("--provenance-json", required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--repo-root", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    try:
        args = _parse_args(argv)
        if args.command == "create":
            provenance = create_provenance(args.candidate_path, args.repo_root)
            Path(args.output).write_text(
                json.dumps(provenance, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(provenance, sort_keys=True))
        else:
            print(json.dumps(
                verify_provenance(
                    args.candidate_path,
                    args.provenance_json,
                    args.expected_commit,
                    args.repo_root,
                ),
                sort_keys=True,
            ))
    except ProvenanceError as exc:
        print("%s: %s" % (exc.code, exc.message), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
