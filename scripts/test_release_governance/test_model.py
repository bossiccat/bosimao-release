"""RED tests for the release governance claim model (Task 1).

These tests define the wished-for API of scripts.release_governance.model.
Run them first: they must FAIL because the module does not exist yet.
"""

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
