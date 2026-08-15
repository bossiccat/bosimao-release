"""Static contract tests for the repository release-governance workflow."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-governance.yml"
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
GITIGNORE = REPO_ROOT / ".gitignore"
HARNESS_DOC = REPO_ROOT / "docs" / "governance" / "release-harness.md"
PROVENANCE_HELPER = REPO_ROOT / "scripts" / "release_governance" / "verify_candidate_provenance.py"
PROVENANCE_TEST = REPO_ROOT / "scripts" / "test_release_governance" / "test_candidate_provenance.py"


def _workflow():
    return WORKFLOW.read_text(encoding="utf-8")


def _harness_doc():
    return HARNESS_DOC.read_text(encoding="utf-8")


def test_pull_requests_run_governance_verify_and_regression_test_jobs():
    workflow = _workflow()

    assert "pull_request:" in workflow
    assert "build-candidate:" in workflow
    assert "verify:" in workflow
    assert "test:" in workflow
    assert "release:" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "github.ref_type == 'tag'" in workflow
    assert "startsWith(github.ref_name, 'v')" in workflow
    assert "needs: build-candidate" in workflow
    assert "needs: [build-candidate, verify, test]" in workflow
    assert "PR 仍运行无密钥回归测试" in _harness_doc()


def test_candidate_is_built_once_and_verified_by_sha_in_verify_and_release():
    workflow = _workflow()

    assert "git archive --format=tar.gz" in workflow
    assert "release-candidate.tar.gz.sha256" in workflow
    assert workflow.count("name: release-candidate") >= 3
    assert workflow.count("sha256sum --check") == 2
    assert workflow.count("--artifact-path \"${{ runner.temp }}/release-input/release-candidate.tar.gz\"") == 2
    assert "release-candidate.tar.gz" in workflow
    assert "release-candidate.tar.gz.sha256" in workflow


def test_candidate_provenance_verifier_is_versioned_tested_and_enforced_before_preflight():
    workflow = _workflow()

    assert PROVENANCE_HELPER.is_file()
    assert PROVENANCE_TEST.is_file()
    assert "release-candidate.provenance.json" in workflow
    assert workflow.count("scripts/release_governance/verify_candidate_provenance.py") >= 3
    assert workflow.count("ref: ${{ github.sha }}") == 2
    assert workflow.count("sha256sum --check release-candidate.tar.gz.sha256") == 2
    assert workflow.index("Verify candidate provenance") < workflow.index("Verify release claims")
    assert workflow.index("Verify candidate provenance", workflow.index("  release:")) < workflow.index(
        "Create release evidence and manifest"
    )


def test_release_is_production_scoped_and_keeps_hmac_secret_out_of_verify_jobs():
    workflow = _workflow()

    release_section = workflow.split("  release:", 1)[1]
    earlier_jobs = workflow.split("  release:", 1)[0]
    assert "environment: production" in release_section
    assert "RELEASE_EVIDENCE_HMAC_KEY" not in earlier_jobs

    release_header, release_steps = release_section.split("    steps:", 1)
    assert "RELEASE_EVIDENCE_HMAC_KEY" not in release_header
    preflight_step = release_steps.split("- name: Create release evidence and manifest", 1)[1]
    preflight_step = preflight_step.split("- name: Upload release evidence and manifest", 1)[0]
    assert "env:" in preflight_step
    assert "RELEASE_EVIDENCE_HMAC_KEY: ${{ secrets.RELEASE_EVIDENCE_HMAC_KEY }}" in preflight_step


def test_release_preflight_follows_successful_verify_build_and_uploads_evidence_manifest():
    workflow = _workflow()

    assert workflow.index("python scripts/release-preflight.py verify") < workflow.index(
        "python scripts/release-preflight.py release"
    )
    assert "if: success()" in workflow
    assert "${{ runner.temp }}/release-candidate.tar.gz" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4" in workflow
    assert "artifacts/release-evidence/" in workflow
    assert "release-manifest.json" in workflow


def test_workflow_cannot_upgrade_claims_to_verified_and_pins_actions():
    workflow = _workflow()

    assert "Verified" not in workflow
    assert "git add governance/claims" not in workflow
    assert "git commit" not in workflow
    assert "git push" not in workflow
    assert "persist-credentials: false" in workflow
    assert "@v" not in workflow
    for action_sha in (
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4",
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4",
    ):
        assert action_sha in workflow


def test_codeowners_protect_governance_release_scripts_and_workflow():
    owners = CODEOWNERS.read_text(encoding="utf-8")

    for protected_path in (
        "/governance/",
        "/scripts/release_governance/",
        "/scripts/release-preflight.py",
        "/.github/workflows/release-governance.yml",
        "/.github/CODEOWNERS",
        "/docs/governance/release-harness.md",
    ):
        assert protected_path in owners


def test_codeowners_uses_real_repository_owner_not_placeholder_team():
    owners = CODEOWNERS.read_text(encoding="utf-8")

    assert "@release-governance-maintainers" not in owners
    assert "@jinhong1688" in owners


def test_gitignore_only_adds_generated_release_evidence_directory():
    gitignore = GITIGNORE.read_text(encoding="utf-8")

    assert "artifacts/release-evidence/" in gitignore


def test_documentation_keeps_unverified_github_settings_at_local_only():
    document = HARNESS_DOC.read_text(encoding="utf-8")

    assert "bossiccat/bosimao-release" in document
    assert "GitHub Free 私有组织仓库" in document
    assert "LOCAL_ONLY" in document
    assert "production environment" in document
    assert "`v*` tag" in document
    assert "当前套餐未提供 required reviewer" in document
    assert "@jinhong1688" in document
    assert "owner 与 reviewer 分离" in document
    assert "Require review from Code Owners" in document
    assert "@release-governance-maintainers" not in document


def test_documentation_does_not_call_preflight_only_release_production_ready():
    document = HARNESS_DOC.read_text(encoding="utf-8")

    assert "当前 release job 只完成 preflight 与证据上传" in document
    assert "官方发布命令与最小权限发布凭据未接入" in document
    assert "凭据锁定目标未完成" in document
    assert "production-ready" not in document
