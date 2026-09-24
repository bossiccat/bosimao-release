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
    # 2026-09-19：契约层门禁接进发布链 —— 契约红则 tag 发布也出不去。
    assert "needs: [build-candidate, verify, test, contract-gate]" in workflow
    assert "PR 仍运行无密钥回归测试" in _harness_doc()


def test_pull_requests_run_the_contract_and_integration_gates():
    """契约层与集成层必须在 PR 上执行，且必须挡在 release 前面。

    这条断言存在的理由（2026-09-19 审计）：
      `backend/tests/contract`（59 文件 / 587 用例）此前只被 deploy-cloudrun.yml 引用，
      而那个 workflow 的触发器是 workflow_dispatch（需人工填 confirm=deploy）与
      tag push，**没有 pull_request**。于是"守护控制面的核心契约层"在整条 PR 流程里
      从不执行：回归拦不住，只能靠人手动发一次部署才知道。
      守卫存在但没有自动入口 = 守卫不存在。故此处把"它在 PR 上跑"钉成契约。
    """
    workflow = _workflow()

    assert "contract-gate:" in workflow
    assert "needs: [build-candidate, verify, test, contract-gate]" in workflow
    assert "python -m pytest backend/tests/contract backend/tests/integration -q" in workflow
    # 依赖清单必须是唯一真源那个文件，而不是在本 workflow 里再抄一份。
    assert "-r ci/test-requirements.txt" in workflow
    # 门禁步骤不得带 if / continue-on-error（本 workflow 只允许 release job 有 if）。
    # 断言只看**去注释后的代码行**：这个仓已经两次栽在"记录规则的散文自己命中了被禁
    # 写法"上（见本文件 test_workflow_cannot_upgrade_claims_to_verified_and_pins_actions
    # 上方注释），所以这里不拿原文做子串扫描。
    gate_section = workflow.split("  contract-gate:", 1)[1].split("\n  release:", 1)[0]
    gate_code = "\n".join(
        line for line in gate_section.splitlines() if not line.strip().startswith("#")
    )
    assert "continue-on-error" not in gate_code
    assert "if:" not in gate_code


def test_windows_leg_runs_the_suites_no_other_workflow_executes():
    """两条此前"任何自动触发都不执行"的守卫必须在 Windows 腿真跑。

    这条断言存在的理由（2026-09-19 审计）：
      · `scripts/test/`（17 文件 / 177 用例）与 `backend/tests/unit`（84 文件 / 789 用例）
        此前**没有任何 workflow 执行它们**；
      · 二者都只能放 Windows 腿：unit 里 test_tts_edge.py 需要 edge-tts、
        test_transcript_storage.py 需要 pywin32（Linux 无 wheel）；scripts/test 里
        12 个用例在非 win32 平台会静默跳过（放 ubuntu 只会拿到更薄的绿）。
      同时钉住"不许用 importorskip 把守卫静默跳过"——真装依赖才是让守卫继续守。
    """
    workflow = _workflow()

    assert "python -m pytest backend/tests/unit -q" in workflow
    assert "node --test scripts/test/*.test.js scripts/test/*.test.mjs" in workflow
    assert "-r ci/test-requirements-windows.txt" in workflow


def test_windows_leg_runs_the_rust_integration_test_targets():
    """`pet-ui/src-tauri/tests/` 的集成目标必须在 Windows 腿真跑。

    这条断言存在的理由（2026-09-24 审计）：
      CI 一直跑的是 `cargo test --lib` —— **`--lib` 只构建 lib 目标，不编译 `tests/`**。
      于是 `pet-ui/src-tauri/tests/` 下 15 个集成测试目标（+ `support.rs` 共享助手）
      虽然进了版本控制、也能编译，却**从未在任何自动触发上执行过**。
      又一例"守卫存在但没有入口 = 守卫不存在"。
    另外钉住两个必需细节，防止有人"简化"成跑不动的写法：
      · 必须带 `--features credential-test-support`：o020_probe_contract.rs 用
        `env!("CARGO_BIN_EXE_o020_credential_probe")`，该 bin 有 required-features，
        不开 feature 连编译都过不去；
      · 必须保留不带 feature 的 `cargo test --lib`（覆盖发布配置下的 lib 单测）。
    """
    workflow = _workflow()

    assert "cargo test --release --tests --features credential-test-support" in workflow
    assert "cargo test --lib" in workflow, (
        "不带 feature 的 `cargo test --lib` 覆盖的是发布配置（feature 关）下的 lib 单测，不得被替换掉"
    )


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


def test_gitignore_only_adds_generated_release_evidence_directory():
    gitignore = GITIGNORE.read_text(encoding="utf-8")

    assert "artifacts/release-evidence/" in gitignore


def test_documentation_keeps_unverified_github_settings_at_local_only():
    document = HARNESS_DOC.read_text(encoding="utf-8")

    assert "GitHub Free 私有仓库" in document
    assert "LOCAL_ONLY" in document
    assert "未验证" in document
    assert "production environment reviewer" in document
    assert "@release-governance-maintainers" in document
    assert "@organization/team" in document
    assert "Require review from Code Owners" in document


def test_documentation_does_not_call_preflight_only_release_production_ready():
    document = HARNESS_DOC.read_text(encoding="utf-8")

    assert "当前 release job 只完成 preflight 与证据上传" in document
    assert "官方发布命令与最小权限发布凭据未接入" in document
    assert "凭据锁定目标未完成" in document
    assert "production-ready" not in document
