"""RED tests for the release governance claim model (Task 1).

These tests define the wished-for API of scripts.release_governance.model.
Run them first: they must FAIL because the module does not exist yet.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from scripts.release_governance.model import (
    ValidationError,
    attempt_fingerprint,
    validate_claim_shape,
    validate_transition,
    validate_verified_claim,
    validate_cancelled_claim,
)


NOW = datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)


def _min_policy():
    return {
        "schema_version": 1,
        "required_claim_ids": ["windows-popup-free", "android-duplex-audio"],
        "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
        "max_attempts_same_fingerprint": 2,
        "max_evidence_age_hours": 72,
        "required_checks": ["sidecar-verify", "tauri-release-build"],
        "release_channel": "production",
    }


def _verified_claim(**overrides):
    claim = {
        "claim_id": "windows-popup-free",
        "state": "Verified",
        "owner": "impl-team",
        "reviewer": "independent-qa",
        "target": {
            "artifact_commit": "abc123",
            "artifact_sha256": "sha256:deadbeef",
        },
        "evidence": [
            {
                "kind": "windows-field",
                "collected_at": "2026-08-15T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "raw_sha256": "sha256:evidence1",
            }
        ],
    }
    claim.update(overrides)
    return claim


def test_fingerprint_is_stable_and_distinct():
    a1 = attempt_fingerprint("root-cause-A", "method-X", "target-1")
    a2 = attempt_fingerprint("root-cause-A", "method-X", "target-1")
    b = attempt_fingerprint("root-cause-A", "method-Y", "target-1")
    assert a1 == a2
    assert a1 != b


def test_verified_claim_passes_when_fully_bound():
    claim = _verified_claim()
    validate_verified_claim(
        claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
    )


def test_missing_artifact_sha256_rejected():
    claim = _verified_claim()
    del claim["target"]["artifact_sha256"]
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "SHA256" in exc.value.code


def test_commit_mismatch_rejected():
    claim = _verified_claim()
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="different-commit", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "COMMIT" in exc.value.code


def test_expired_evidence_rejected():
    claim = _verified_claim()
    claim["evidence"][0]["expires_at"] = "2026-08-01T00:00:00Z"
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "EXPIRED" in exc.value.code


def test_reviewer_equal_owner_rejected():
    claim = _verified_claim()
    claim["reviewer"] = claim["owner"]
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "REVIEWER" in exc.value.code


def test_p0_claim_must_be_verified():
    claim = _verified_claim(state="EvidencePending")
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "VERIFIED" in exc.value.code


def test_missing_evidence_rejected():
    claim = _verified_claim()
    claim["evidence"] = []
    with pytest.raises(ValidationError) as exc:
        validate_verified_claim(
            claim, _min_policy(), NOW, expected_commit="abc123", expected_artifact_sha256="sha256:deadbeef"
        )
    assert "EVIDENCE" in exc.value.code


def test_transition_allows_valid_step():
    validate_transition("Draft", "Ready")


def test_transition_rejects_terminal_to_draft():
    with pytest.raises(ValidationError):
        validate_transition("Verified", "Draft")


def test_transition_rejects_skip():
    with pytest.raises(ValidationError):
        validate_transition("Draft", "Verified")


def test_cancelled_requires_superseded_by():
    claim = {
        "claim_id": "windows-popup-free",
        "state": "Cancelled",
        "superseded_by": None,
    }
    with pytest.raises(ValidationError) as exc:
        validate_cancelled_claim(claim)
    assert "SUPERSEDED" in exc.value.code


def test_cancelled_with_superseded_by_passes():
    claim = {
        "claim_id": "windows-popup-free",
        "state": "Cancelled",
        "superseded_by": "windows-popup-free-v2",
    }
    validate_cancelled_claim(claim)


def test_shape_requires_claim_id():
    with pytest.raises(ValidationError):
        validate_claim_shape({"state": "Draft"})


def test_shape_rejects_unknown_state():
    with pytest.raises(ValidationError):
        validate_claim_shape({"claim_id": "x", "state": "NotARealState"})


# --- 时效判据必须以「采集时刻」为锚，而不是以声明被写入的时刻为锚 ---
# 缺陷：expires_at 由写入时刻导出 ⇒ 一份采集于很久以前的证据，只要声明是刚写的，
# 就能拿到一个"新鲜"的 expires_at 并通关。下面这组测试钉的就是这条假绿通道。


def _evidence(claim):
    return claim["evidence"][0]


def _validate(claim, policy=None):
    return validate_verified_claim(
        claim,
        policy or _min_policy(),
        NOW,
        expected_commit="abc123",
        expected_artifact_sha256="sha256:deadbeef",
    )


def test_stale_evidence_rejected_even_when_expires_at_is_in_the_future():
    """在验证时刻，collected_at + max_evidence_age_hours <= now ⇒ 过期，哪怕 expires_at 在未来。"""
    claim = _verified_claim()
    _evidence(claim)["collected_at"] = "2026-08-01T00:00:00Z"  # NOW 前 14 天
    _evidence(claim)["expires_at"] = "2099-01-01T00:00:00Z"  # "很新鲜"，但是伪造的
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_STALE"


def test_evidence_at_exact_max_age_boundary_is_stale():
    """collected_at + 72h == now ⇒ 判过期（闭区间）。"""
    claim = _verified_claim()
    _evidence(claim)["collected_at"] = "2026-08-12T00:00:00Z"  # +72h 恰好等于 NOW
    _evidence(claim)["expires_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_STALE"


def test_evidence_just_inside_max_age_is_accepted():
    """collected_at + 72h > now ⇒ 仍新鲜（判据不放宽成更早失效）。"""
    claim = _verified_claim()
    _evidence(claim)["collected_at"] = "2026-08-12T00:00:01Z"
    _evidence(claim)["expires_at"] = "2099-01-01T00:00:00Z"
    _validate(claim)


def test_evidence_collected_in_the_future_rejected():
    """未来时间点是一种伪造 —— 必须拒绝，而不是当作"刚刚采集"。"""
    claim = _verified_claim()
    _evidence(claim)["collected_at"] = "2026-08-16T00:00:00Z"
    _evidence(claim)["expires_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_FROM_FUTURE"


def test_unparseable_collected_at_rejected():
    claim = _verified_claim()
    _evidence(claim)["collected_at"] = "yesterday-ish"
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "BAD_TIMESTAMP"


def test_missing_collected_at_rejected():
    claim = _verified_claim()
    del _evidence(claim)["collected_at"]
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "BAD_TIMESTAMP"


def test_naive_collected_at_rejected():
    claim = _verified_claim()
    _evidence(claim)["collected_at"] = "2026-08-15T00:00:00"  # 无时区
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "BAD_TIMESTAMP"


def test_policy_without_max_evidence_age_is_rejected_fail_closed():
    """判不了新鲜度就不能放行（fail-closed），而不是默认通过。"""
    claim = _verified_claim()
    policy = _min_policy()
    del policy["max_evidence_age_hours"]
    with pytest.raises(ValidationError) as exc:
        _validate(claim, policy)
    assert exc.value.code == "BAD_MAX_EVIDENCE_AGE"


def test_non_integer_max_evidence_age_is_rejected_fail_closed():
    claim = _verified_claim()
    policy = _min_policy()
    policy["max_evidence_age_hours"] = "72"
    with pytest.raises(ValidationError) as exc:
        _validate(claim, policy)
    assert exc.value.code == "BAD_MAX_EVIDENCE_AGE"


# --- 证据等级（kind）必须存在且落在 policy.allowed_evidence_kinds 白名单内 ---
# 缺陷：校验侧此前对 evidence[].kind 零引用（model.py / verify.py 里检索不到 kind）
# ⇒ policy 声明了 allowed_evidence_kinds，但门禁从未执行过这条控制。
# 后果：一条 claim 可以把证据标成任意等级名（含白名单外的伪造名，例如把现场证据
# 冒充成会被 HMAC 封存的 ci-command），甚至干脆不写 kind，门禁都不拦。
# 下面这组测试钉的就是这条"证据等级伪造 / 缺省绕过"通道。


def test_evidence_kind_outside_policy_whitelist_is_rejected():
    claim = _verified_claim()
    _evidence(claim)["kind"] = "totally-forged-kind"
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_KIND_NOT_ALLOWED"
    # 错误信息必须指出是哪个 kind、白名单是什么。
    assert "totally-forged-kind" in exc.value.message
    assert "ci-command" in exc.value.message


def test_claim_evidence_kinds_exclude_ci_command_in_the_real_policy():
    """`ci-command` 不属于 **claim 的**证据类别 —— 这是按构造关掉一条等级伪造通道。

    背景（2026-09-21 实测）：claim 的 `evidence[]` 曾经也允许 `kind="ci-command"`，
    但校验侧**结构上无法**核实它 —— `verify_claims` 拿不到 `--evidence-root` /
    `--release-id`（见 `release-preflight.py` 的 `_verify()` 入参），而 HMAC 封存的
    `result.json` 只在 `release` 分支的 `_load_required_results()` 里被读。
    于是"一条没有 HMAC 的现场日志自称 `ci-command`"会被放行 —— 而 `ci-command` 与
    两个 field 类的**语义差别恰恰就是有没有 HMAC**。

    `ci-command` 的真实身份是 **locked check 的 `evidence_class`**（`command-lock.json`）
    与封存结果的目录名（`<release_id>/ci-command/<check_id>/result.json`）—— 它有自己
    的通道，不经 claim。

    所以把它从 claim 白名单移除：**一个没有任何东西能验证的标签，不该能被声明。**
    （若将来真要允许 claim 携带 ci-command 证据，必须**连同校验一起**加回来 —— 那时的
    前提是那层能拿到 sealed 结果。单独把标签加回来就是重新打开这条通道。）
    """
    policy_path = Path(__file__).resolve().parents[2] / "governance" / "release-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert set(policy["allowed_evidence_kinds"]) == {"windows-field", "android-field"}, (
        policy["allowed_evidence_kinds"]
    )


def test_ci_command_labelled_claim_evidence_is_rejected():
    """按真实白名单，claim 写 `kind="ci-command"` 必须被拒。"""
    claim = _verified_claim()
    _evidence(claim)["kind"] = "ci-command"
    policy = _min_policy()
    policy["allowed_evidence_kinds"] = ["windows-field", "android-field"]
    with pytest.raises(ValidationError) as exc:
        _validate(claim, policy)
    assert exc.value.code == "EVIDENCE_KIND_NOT_ALLOWED"
    assert "ci-command" in exc.value.message


def test_evidence_kind_missing_is_rejected():
    """不写 kind 不能成为绕过白名单的后门。"""
    claim = _verified_claim()
    del _evidence(claim)["kind"]
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_KIND_NOT_ALLOWED"


def test_evidence_kind_empty_is_rejected():
    claim = _verified_claim()
    _evidence(claim)["kind"] = ""
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_KIND_NOT_ALLOWED"


def test_evidence_kind_non_string_is_rejected():
    claim = _verified_claim()
    _evidence(claim)["kind"] = 123
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "EVIDENCE_KIND_NOT_ALLOWED"


def test_policy_without_allowed_evidence_kinds_is_rejected_fail_closed():
    """判不出白名单就不能放行（fail-closed），与 BAD_MAX_EVIDENCE_AGE 同口径。"""
    claim = _verified_claim()
    policy = _min_policy()
    del policy["allowed_evidence_kinds"]
    with pytest.raises(ValidationError) as exc:
        _validate(claim, policy)
    assert exc.value.code == "BAD_ALLOWED_EVIDENCE_KINDS"


def test_policy_with_non_list_allowed_evidence_kinds_is_rejected_fail_closed():
    claim = _verified_claim()
    policy = _min_policy()
    policy["allowed_evidence_kinds"] = "ci-command"  # 字符串不是列表
    with pytest.raises(ValidationError) as exc:
        _validate(claim, policy)
    assert exc.value.code == "BAD_ALLOWED_EVIDENCE_KINDS"


@pytest.mark.parametrize("kind", ["ci-command", "windows-field", "android-field"])
def test_each_declared_kind_is_accepted(kind):
    """白名单里的每个 kind 都必须被接受 —— 防止判据被收得过紧。"""
    claim = _verified_claim()
    _evidence(claim)["kind"] = kind
    _validate(claim)


# ── 场景覆盖（2026-09-24 实测事故的永久守卫）────────────────────────────────
# `required_scenarios` 此前是**纯人读字段**，模型从不读它。于是 windows-popup-free
# 的场景 6「正常退出后重启」可以用 `taskkill /F` 近似、证据报告自己写着
# 「不得据此声称已覆盖」，而门禁照样把这条 claim 判成 Verified ——
# 即"声明得比证据更强"在机械上无法被发现。以下用例钉住新判据。


def _claim_with_scenarios(scenarios, coverage_per_entry):
    """构造声明了 required_scenarios 的 Verified claim；每条证据带一份 scenario_coverage。"""
    evidence = []
    for coverage in coverage_per_entry:
        entry = {
            "kind": "windows-field",
            "collected_at": "2026-08-15T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "raw_sha256": "sha256:evidence",
        }
        if coverage is not None:
            entry["scenario_coverage"] = coverage
        evidence.append(entry)
    return _verified_claim(required_scenarios=list(scenarios), evidence=evidence)


def test_all_declared_scenarios_pass_is_accepted():
    """阴性对照：逐条 PASS 时判据必须安静 —— 否则它就是"对什么都红"。"""
    claim = _claim_with_scenarios(
        ["首次启动", "正常退出后重启"],
        [{"首次启动": "PASS", "正常退出后重启": "PASS"}],
    )
    _validate(claim)


def test_declared_scenario_absent_from_coverage_is_rejected():
    """场景没被任何证据声明覆盖 ⇒ 判红，且错误信息点名是哪个场景。"""
    claim = _claim_with_scenarios(["首次启动", "正常退出后重启"], [{"首次启动": "PASS"}])
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "SCENARIO_COVERAGE_INCOMPLETE"
    assert "正常退出后重启=NOT_DECLARED" in str(exc.value)


def test_declared_scenario_with_non_pass_verdict_is_rejected():
    """**本次事故的形状**：场景被声明为未覆盖，就不得算过。"""
    claim = _claim_with_scenarios(["正常退出后重启"], [{"正常退出后重启": "NOT_COVERED"}])
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "SCENARIO_COVERAGE_INCOMPLETE"
    assert "正常退出后重启=NOT_COVERED" in str(exc.value)


def test_contradictory_verdicts_count_as_not_covered():
    """同一场景一条证据说 PASS、另一条说未覆盖 ⇒ 从严判未覆盖（不取"有一条 PASS 就算过"）。"""
    claim = _claim_with_scenarios(["首次启动"], [{"首次启动": "PASS"}, {"首次启动": "NOT_COVERED"}])
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "SCENARIO_COVERAGE_INCOMPLETE"


def test_coverage_can_be_split_across_evidence_entries():
    """覆盖允许分散在多条证据里 —— 判据是"并集是否逐条 PASS"。"""
    claim = _claim_with_scenarios(["A", "B"], [{"A": "PASS"}, {"B": "PASS"}])
    _validate(claim)


def test_evidence_without_coverage_key_does_not_claim_coverage():
    """证据不写 scenario_coverage ⇒ 不主张覆盖任何场景，仍须逐条有 PASS。"""
    claim = _claim_with_scenarios(["A"], [None])
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "SCENARIO_COVERAGE_INCOMPLETE"
    assert "A=NOT_DECLARED" in str(exc.value)


def test_bad_scenario_coverage_shape_is_rejected_fail_closed():
    """形状不对（不是对象）要报 BAD_SCENARIO_COVERAGE，而不是崩成 TypeError。"""
    claim = _claim_with_scenarios(["A"], [[]])
    with pytest.raises(ValidationError) as exc:
        _validate(claim)
    assert exc.value.code == "BAD_SCENARIO_COVERAGE"


def test_claim_without_required_scenarios_is_unaffected():
    """未声明 required_scenarios 的 claim 不受这条判据影响（向后兼容）。"""
    _validate(_verified_claim())
