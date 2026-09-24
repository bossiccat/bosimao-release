"""契约：记录 P0 证据的工具必须**写盘前拦住机械错误**，且不能凭空造证据。

为什么单独钉它
--------------
P0 声明是商业发布的闸门。手改 `governance/claims/*.json` 时最容易错的是
**证据哈希**与**时效窗口**两处，而错了的后果很隐蔽：门禁悄悄不通过，
或者"看着是 Verified、实则无效"。

所以本契约要证明的是**拒止能力**，不是"能写出文件"：
- 它算出的哈希必须等于证据文件真实 sha256（不能凭空填）；
- `reviewer == owner`、`kind` 不在白名单、证据文件不存在、覆盖已 Verified 声明
  —— 这四种都必须**拒绝并返回非零**；
- 写出的声明必须能被**治理层自己的校验器**接受（而不是本工具自说自话）。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "record-release-evidence.py"
sys.path.insert(0, str(ROOT))

from scripts.release_governance.model import validate_verified_claim  # noqa: E402
from scripts.release_governance.verify import _validate_evidence_hashes  # noqa: E402

COMMIT = "c" * 40


def _fixture(tmp_path: Path):
    (tmp_path / "claims").mkdir()
    (tmp_path / "evidence.log").write_text("真机现场取证记录", encoding="utf-8")
    (tmp_path / "artifact.bin").write_bytes(b"release-candidate-bytes")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1,
        "required_claim_ids": ["android-duplex-audio", "windows-popup-free"],
        "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
        "max_attempts_same_fingerprint": 2,
        "max_evidence_age_hours": 72,
        "required_checks": [],
        "release_channel": "production",
    }), encoding="utf-8")
    return policy, tmp_path / "claims", tmp_path / "evidence.log", tmp_path / "artifact.bin"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def _base(policy: Path, claims: Path, evidence: Path, artifact: Path) -> list[str]:
    return ["--claim-id", "android-duplex-audio", "--kind", "android-field",
            "--evidence", str(evidence), "--artifact", str(artifact),
            "--commit", COMMIT, "--owner", "impl-team", "--reviewer", "independent-qa",
            "--policy", str(policy), "--claims-dir", str(claims)]


def test_written_claim_passes_the_governance_validator(tmp_path: Path) -> None:
    """写出的声明必须能被治理层自己的校验器接受。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    proc = _run(*_base(policy, claims, evidence, artifact))
    assert proc.returncode == 0, proc.stderr
    written = json.loads((claims / "android-duplex-audio.json").read_text(encoding="utf-8"))
    from datetime import datetime, timezone
    policy_obj = json.loads(policy.read_text(encoding="utf-8"))
    expected_sha = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    # 用真实校验器验证（不是本工具自证）
    validate_verified_claim(written, policy_obj, datetime.now(timezone.utc), COMMIT, expected_sha)
    assert _validate_evidence_hashes(written) == []


def test_hash_is_the_real_file_hash_not_invented(tmp_path: Path) -> None:
    """哈希必须等于证据文件真实 sha256 —— 不允许凭空填。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    _run(*_base(policy, claims, evidence, artifact))
    written = json.loads((claims / "android-duplex-audio.json").read_text(encoding="utf-8"))
    real = "sha256:" + hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert written["evidence"][0]["raw_sha256"] == real


def test_refuses_reviewer_equal_to_owner(tmp_path: Path) -> None:
    policy, claims, evidence, artifact = _fixture(tmp_path)
    args = _base(policy, claims, evidence, artifact)
    args[args.index("independent-qa")] = "impl-team"
    proc = _run(*args)
    assert proc.returncode == 1, "reviewer == owner 必须拒绝"
    assert not (claims / "android-duplex-audio.json").exists(), "拒绝时不得写盘"


def test_refuses_kind_outside_the_policy_whitelist(tmp_path: Path) -> None:
    policy, claims, evidence, artifact = _fixture(tmp_path)
    args = _base(policy, claims, evidence, artifact)
    args[args.index("android-field")] = "made-up-kind"
    proc = _run(*args)
    assert proc.returncode == 1
    assert not (claims / "android-duplex-audio.json").exists()


def test_refuses_missing_evidence_file(tmp_path: Path) -> None:
    """没有证据文件就必须拒绝 —— 本工具不造证据。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    args = _base(policy, claims, evidence, artifact)
    args[args.index(str(evidence))] = str(tmp_path / "nope.log")
    proc = _run(*args)
    assert proc.returncode == 1
    assert "不会凭空造证据" in proc.stderr
    assert not (claims / "android-duplex-audio.json").exists()


def test_refuses_overwriting_an_already_verified_claim(tmp_path: Path) -> None:
    """已 Verified 的声明不得被静默改写。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    (claims / "android-duplex-audio.json").write_text(
        json.dumps({"claim_id": "android-duplex-audio", "state": "Verified",
                    "owner": "impl-team", "reviewer": "independent-qa"}),
        encoding="utf-8")
    proc = _run(*_base(policy, claims, evidence, artifact))
    assert proc.returncode == 1
    assert "--allow-reverify" in proc.stderr
    # 原文件未被改动
    assert json.loads((claims / "android-duplex-audio.json").read_text(encoding="utf-8"))["owner"] == "impl-team"


def test_records_merge_and_preserve_claim_governance_metadata(tmp_path: Path) -> None:
    """记录证据必须**合并**而非整体覆盖既有声明。

    声明的 `risk` 是"在断言什么"、`required_scenarios` 是"必须演示哪些场景"，
    `legacy_tasks` / `superseded_by` 是溯源，`target.artifact` 描述绑定的是
    哪条产物链，`attempts` 是同一失败指纹的重试历史（`max_attempts_same_fingerprint`
    的电路判据就靠它）。这些都不是本工具该改写的字段，一旦被整体覆盖，
    声明会"门禁变绿但语义消失"——审计者再也看不出原本要证明什么。
    """
    policy, claims, evidence, artifact = _fixture(tmp_path)
    prior = {
        "claim_id": "android-duplex-audio",
        "state": "EvidencePending",
        "owner": "impl-team",
        "reviewer": "independent-qa",
        "risk": "客户现场出现回声/双讲撕裂",
        "target": {
            "artifact_commit": "",
            "artifact_sha256": "",
            "artifact": "packaged app.apk -> AudioEngine -> CapturePath",
        },
        "required_scenarios": ["首次启动", "App 重启", "relay 故障恢复"],
        "legacy_tasks": ["Jax-Audio-1", "jax-audio-2"],
        "evidence": [],
        "superseded_by": "android-duplex-audio-v2",
        "attempts": [
            {"fingerprint": "fp:deadbeef", "outcome": "failed", "note": "第一次现场取证未复现回声"}
        ],
    }
    (claims / "android-duplex-audio.json").write_text(
        json.dumps(prior, ensure_ascii=False), encoding="utf-8")

    # 2026-09-24：该 claim 声明了 3 条 required_scenarios，而门禁现在要求逐条声明覆盖为
    # PASS（SCENARIO_COVERAGE_INCOMPLETE）。本用例关心的是"合并保留"，故这里补齐覆盖，
    # 以便真正走到合并逻辑；覆盖判据本身由 test_records_refuse_when_declared_scenarios_
    # are_not_covered 钉住。
    proc = _run(
        *_base(policy, claims, evidence, artifact),
        "--scenario-coverage", "首次启动=PASS",
        "--scenario-coverage", "App 重启=PASS",
        "--scenario-coverage", "relay 故障恢复=PASS",
    )
    assert proc.returncode == 0, proc.stderr

    written = json.loads((claims / "android-duplex-audio.json").read_text(encoding="utf-8"))

    # 必须原样保留的治理元数据（本缺陷的判据）
    for key in ("risk", "required_scenarios", "legacy_tasks", "superseded_by"):
        assert written.get(key) == prior[key], f"{key} 被覆盖丢失"
    assert written["target"]["artifact"] == prior["target"]["artifact"], \
        "target.artifact 描述被覆盖丢失"
    assert written["attempts"] == prior["attempts"], "attempts 重试历史被清空"

    # 本工具应当改写的字段确实被更新
    assert written["claim_id"] == "android-duplex-audio"
    assert written["state"] == "Verified"
    assert written["owner"] == "impl-team"
    assert written["reviewer"] == "independent-qa"
    assert written["target"]["artifact_commit"] == COMMIT
    assert written["target"]["artifact_sha256"] == (
        "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest())
    assert len(written["evidence"]) == 1
    assert written["evidence"][0]["raw_sha256"] == (
        "sha256:" + hashlib.sha256(evidence.read_bytes()).hexdigest())


# ── 场景覆盖（2026-09-24）────────────────────────────────────────────────────
# 起因：`required_scenarios` 原是纯人读字段，门禁从不读它 —— 于是
# windows-popup-free 的场景 6「正常退出后重启」可以用 taskkill /F 近似、
# 证据报告自己写着「不得据此声称已覆盖」，而 claim 照样能变成 Verified。
# 现在门禁要求逐条 PASS，记录器必须能声明覆盖；以下用例把这条链路钉住。


def _prior_claim_with_scenarios(claims: Path, scenarios) -> None:
    (claims / "android-duplex-audio.json").write_text(
        json.dumps({
            "claim_id": "android-duplex-audio",
            "state": "EvidencePending",
            "owner": "impl-team",
            "reviewer": "independent-qa",
            "risk": "客户现场出现回声/双讲撕裂",
            "target": {"artifact_commit": "", "artifact_sha256": "",
                       "artifact": "packaged app.apk -> AudioEngine -> CapturePath"},
            "required_scenarios": list(scenarios),
            "legacy_tasks": ["Jax-Audio-1"],
            "evidence": [],
            "superseded_by": None,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def test_refuses_when_declared_scenarios_have_no_coverage(tmp_path: Path) -> None:
    """声明了 required_scenarios 却不声明覆盖 ⇒ 必须拒绝写盘，且点名每个场景。

    这正是事故形状：门禁若不拦，"声明得比证据更强"的 claim 会静默落地。
    """
    policy, claims, evidence, artifact = _fixture(tmp_path)
    _prior_claim_with_scenarios(claims, ["首次启动", "正常退出后重启"])
    proc = _run(*_base(policy, claims, evidence, artifact))
    assert proc.returncode == 1, "缺覆盖声明必须拒绝"
    assert "SCENARIO_COVERAGE_INCOMPLETE" in proc.stderr
    assert "正常退出后重启" in proc.stderr, "错误信息必须点名缺哪个场景"
    assert not (claims / "android-duplex-audio.json").read_text(
        encoding="utf-8").count('"Verified"'), "拒绝时不得把 state 写成 Verified"


def test_refuses_partial_coverage_and_names_the_missing_scenario(tmp_path: Path) -> None:
    """只覆盖一部分 ⇒ 拒绝，并把没覆盖的那条名字报出来。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    _prior_claim_with_scenarios(claims, ["首次启动", "App 重启", "正常退出后重启"])
    proc = _run(
        *_base(policy, claims, evidence, artifact),
        "--scenario-coverage", "首次启动=PASS",
        "--scenario-coverage", "App 重启=PASS",
    )
    assert proc.returncode == 1
    assert "正常退出后重启" in proc.stderr


def test_accepts_full_pass_coverage_and_records_it(tmp_path: Path) -> None:
    """阴性对照：逐条 PASS 时必须接受，并把 scenario_coverage 落进证据条目。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    _prior_claim_with_scenarios(claims, ["首次启动", "正常退出后重启"])
    proc = _run(
        *_base(policy, claims, evidence, artifact),
        "--scenario-coverage", "首次启动=PASS",
        "--scenario-coverage", "正常退出后重启=PASS",
    )
    assert proc.returncode == 0, proc.stderr
    written = json.loads((claims / "android-duplex-audio.json").read_text(encoding="utf-8"))
    assert written["evidence"][0]["scenario_coverage"] == {
        "首次启动": "PASS", "正常退出后重启": "PASS"}


def test_refuses_malformed_scenario_coverage_argument(tmp_path: Path) -> None:
    """`--scenario-coverage` 不是 `场景=判定` 形式 ⇒ 输入不可用（EXIT_UNUSABLE），不是静默忽略。"""
    policy, claims, evidence, artifact = _fixture(tmp_path)
    _prior_claim_with_scenarios(claims, ["首次启动"])
    proc = _run(*_base(policy, claims, evidence, artifact),
                "--scenario-coverage", "首次启动")
    assert proc.returncode != 0
    assert "场景=判定" in proc.stderr
