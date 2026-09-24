"""Pure, fail-closed model for release governance claims.

Constraints enforced here:
- No network access, no file I/O, no subprocess.
- All timestamps must be UTC ISO-8601; anything unparseable is rejected.
- A P0 claim (listed in policy.required_claim_ids) must be Verified.
- A Verified claim must bind to the exact commit + artifact SHA-256 and carry
  unexpired evidence reviewed by someone other than its owner.
- Every evidence entry must declare a non-empty `kind` that is a member of
  `policy.allowed_evidence_kinds`; a missing or non-list policy value is
  fail-closed (BAD_ALLOWED_EVIDENCE_KINDS), and a missing/empty/unknown kind is
  EVIDENCE_KIND_NOT_ALLOWED.
- Evidence freshness is anchored at `collected_at`, not at `expires_at`: at the
  verification instant, `collected_at + max_evidence_age_hours <= now` means the
  evidence is stale even if `expires_at` is still in the future. `collected_at`
  must itself be parseable UTC and must not be in the future.
- artifact_commit may instead be an ancestor: when the caller proves (with git)
  that it is an ancestor of the expected commit whose delta touches only the
  claims directory, the proof is passed in as artifact_commit_lineage. The proof
  is caller-supplied and is shape-checked here, not re-computed — same trust
  boundary as expected_commit itself (this module stays pure: no subprocess).
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone

FULL_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")

ALLOWED_STATES = {
    "Draft",
    "Ready",
    "Running",
    "EvidencePending",
    "Review",
    "Verified",
    "Rejected",
    "Blocked",
    "Cancelled",
    "CircuitOpen",
    "Escalated",
}

TRANSITIONS = {
    ("Draft", "Ready"),
    ("Ready", "Running"),
    ("Running", "EvidencePending"),
    ("EvidencePending", "Review"),
    ("Review", "Verified"),
    ("Review", "Rejected"),
    ("Review", "Blocked"),
    ("Rejected", "Ready"),
    ("Rejected", "CircuitOpen"),
    ("CircuitOpen", "Escalated"),
}

# Cancelled may only be entered from a non-Verified state.
CANCELLABLE_FROM = ALLOWED_STATES - {"Verified"}


class ValidationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def attempt_fingerprint(root_cause_key, verification_method, target):
    """Stable fingerprint for the (root cause, method, target) retry triage."""
    raw = "\x00".join([str(root_cause_key), str(verification_method), str(target)])
    return "fp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_utc(value):
    if not isinstance(value, str):
        raise ValidationError("BAD_TIMESTAMP", "timestamp must be a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValidationError("BAD_TIMESTAMP", "cannot parse timestamp %r" % (value,))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError("BAD_TIMESTAMP", "timestamp must be UTC (%r)" % (value,))
    return dt.astimezone(timezone.utc)


def validate_claim_shape(claim):
    if not isinstance(claim, dict):
        raise ValidationError("BAD_SHAPE", "claim must be a JSON object")
    if not claim.get("claim_id"):
        raise ValidationError("MISSING_CLAIM_ID", "claim_id is required")
    if "state" not in claim:
        raise ValidationError("MISSING_STATE", "state is required")
    if claim["state"] not in ALLOWED_STATES:
        raise ValidationError("UNKNOWN_STATE", "unknown state %r" % (claim["state"],))


def validate_transition(previous_state, next_state):
    if previous_state not in ALLOWED_STATES or next_state not in ALLOWED_STATES:
        raise ValidationError("UNKNOWN_STATE", "illegal state in transition")
    if (previous_state, next_state) not in TRANSITIONS:
        raise ValidationError(
            "ILLEGAL_TRANSITION",
            "cannot transition %s -> %s" % (previous_state, next_state),
        )
    if next_state == "Cancelled" and previous_state not in CANCELLABLE_FROM:
        raise ValidationError(
            "ILLEGAL_TRANSITION",
            "Verified claim cannot be cancelled (%s -> Cancelled)" % (previous_state,),
        )


def validate_verified_claim(
    claim,
    policy,
    now_utc,
    expected_commit,
    expected_artifact_sha256,
    artifact_commit_lineage=None,
):
    validate_claim_shape(claim)

    required = policy.get("required_claim_ids", [])
    if claim["claim_id"] in required and claim["state"] != "Verified":
        raise ValidationError(
            "NOT_VERIFIED",
            "P0 claim %s is %s, not Verified" % (claim["claim_id"], claim["state"]),
        )

    target = claim.get("target") or {}
    if not target.get("artifact_sha256"):
        raise ValidationError("MISSING_ARTIFACT_SHA256", "target.artifact_sha256 is required")
    if artifact_commit_lineage is None:
        if target.get("artifact_commit") != expected_commit:
            raise ValidationError(
                "COMMIT_MISMATCH",
                "artifact_commit %r != expected %r"
                % (target.get("artifact_commit"), expected_commit),
            )
    else:
        _validate_artifact_commit_lineage(
            target.get("artifact_commit"), expected_commit, artifact_commit_lineage
        )
    if target["artifact_sha256"] != expected_artifact_sha256:
        raise ValidationError(
            "SHA256_MISMATCH",
            "artifact_sha256 %r != expected %r" % (target["artifact_sha256"], expected_artifact_sha256),
        )

    if claim.get("reviewer") and claim.get("owner") and claim["reviewer"] == claim["owner"]:
        raise ValidationError(
            "REVIEWER_IS_OWNER",
            "reviewer must differ from owner (%r)" % (claim["owner"],),
        )

    evidence = claim.get("evidence") or []
    if not evidence:
        raise ValidationError("MISSING_EVIDENCE", "Verified claim requires at least one evidence record")
    max_age_hours = policy.get("max_evidence_age_hours")
    if not isinstance(max_age_hours, int) or isinstance(max_age_hours, bool):
        # 判不出新鲜度就不能放行：缺 policy 值不是"没规则"，而是"无法验证"。
        raise ValidationError(
            "BAD_MAX_EVIDENCE_AGE",
            "policy.max_evidence_age_hours must be an integer number of hours (got %r)" % (max_age_hours,),
        )
    allowed_kinds = policy.get("allowed_evidence_kinds")
    if not isinstance(allowed_kinds, list):
        # 同 BAD_MAX_EVIDENCE_AGE 的口径：判不出白名单就不能放行。
        # policy 声明了 allowed_evidence_kinds，校验侧就必须执行它 —— 否则这条控制
        # 只是"写在 policy 里"而已（本仓反复清的就是这一类）。
        # 另外这道形状检查还挡住了一个更坏的失败模式：policy 缺该键时
        # `kind not in None` 会抛 TypeError，而 verify_claims 只捕获 ValidationError
        # ⇒ 整个验证会以未捕获异常崩掉，而不是干净地 fail-closed。
        raise ValidationError(
            "BAD_ALLOWED_EVIDENCE_KINDS",
            "policy.allowed_evidence_kinds must be a list of strings (got %r)" % (allowed_kinds,),
        )
    for idx, ev in enumerate(evidence):
        if not isinstance(ev, dict):
            raise ValidationError("BAD_EVIDENCE", "evidence[%d] must be an object" % idx)
        # 证据等级必须显式声明且落在 policy 白名单内。
        # 缺 kind / 空 kind 一律判红：否则"不写等级"就成了绕过白名单的后门。
        # 放在时效判据之前 —— 等级是证据的身份，先定身份再谈新鲜度。
        kind = ev.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValidationError(
                "EVIDENCE_KIND_NOT_ALLOWED",
                "evidence[%d].kind is missing or empty; policy.allowed_evidence_kinds=%s"
                % (idx, allowed_kinds),
            )
        if kind not in allowed_kinds:
            raise ValidationError(
                "EVIDENCE_KIND_NOT_ALLOWED",
                "evidence[%d].kind %r is not in policy.allowed_evidence_kinds %s"
                % (idx, kind, allowed_kinds),
            )
        # 时效锚在**采集时刻**，不锚在"声明被写入的时刻"。
        # 否则一份采集于很久以前的证据，只要声明是刚写的，就能拿到一个新鲜的
        # expires_at 并通关 —— 那是"看起来在有效期内，其实早已过期"的假绿通道。
        # expires_at 只是它的一次性快照，因此这里单独校验、绝不拿它替代本判据。
        collected_at = _parse_utc(ev.get("collected_at"))
        if collected_at > now_utc:
            raise ValidationError(
                "EVIDENCE_FROM_FUTURE",
                "evidence[%d] collected_at %s is in the future (now %s)"
                % (idx, ev.get("collected_at"), now_utc.isoformat()),
            )
        if collected_at + timedelta(hours=max_age_hours) <= now_utc:
            raise ValidationError(
                "EVIDENCE_STALE",
                "evidence[%d] collected at %s is older than %sh at %s: %s"
                % (
                    idx,
                    ev.get("collected_at"),
                    max_age_hours,
                    now_utc.isoformat(),
                    (collected_at + timedelta(hours=max_age_hours)).isoformat(),
                ),
            )
        expires_at = _parse_utc(ev.get("expires_at"))
        if expires_at <= now_utc:
            raise ValidationError(
                "EVIDENCE_EXPIRED",
                "evidence[%d] expired at %s" % (idx, ev.get("expires_at")),
            )

    # ── 场景覆盖：声明的 required_scenarios 必须**逐条**被证据覆盖为 PASS ──────────
    # 为什么需要这一条（2026-09-24 实测事故，不是预防性设计）：
    #   `required_scenarios` 此前是**纯人读字段** —— 本模块从头到尾没有读过它。
    #   于是 windows-popup-free 的现场证据报告可以自述
    #   「6/6 PASS 是真的，但它不是『6 条 required_scenario 全部字面满足』」：
    #   其中场景 6「正常退出后重启」是用 `taskkill /F` **近似**的，证据报告自己写着
    #   「该路径（tray 优雅退出）本次未被执行，属未覆盖项，**不得据此声称已覆盖**」——
    #   而门禁照样会把这条 claim 判成 Verified。
    #   即：**一条声明得比证据更强的 claim，机械上无法被发现。**（与人读字段其它
    #   同类问题同族：C-4 的四个人读字段。）
    #
    # 口径：**只有显式 PASS 才算覆盖**；同一场景若在任一条证据里被声明为非 PASS，
    #   即视为未覆盖（严格取交集方向，不取"有一条 PASS 就算过"）。
    #   覆盖不了只有两条路，且两条都是**可见动作**：
    #     ① 补测，把该场景真做掉；
    #     ② 改 claim.required_scenarios，把要求收窄到实际验证过的范围
    #        —— 改动会落在 diff 里，由独立 reviewer 复核。
    # 刻意**不提供** "ACCEPTED_LIMITATION / PARTIAL 也算过" 的档位：那等于把"近似"
    #   合法化。本次用户明确选择的是「不接受带限定披露后归档」，故不设该后门。
    required_scenarios = claim.get("required_scenarios") or []
    if required_scenarios:
        declared: dict = {}
        for idx, ev in enumerate(evidence):
            coverage = ev.get("scenario_coverage")
            if coverage is None:
                # 不声明覆盖 = 这条证据不主张覆盖任何场景。若最终无人覆盖，下面会报。
                continue
            if not isinstance(coverage, dict):
                raise ValidationError(
                    "BAD_SCENARIO_COVERAGE",
                    "evidence[%d].scenario_coverage must be an object mapping scenario -> verdict (got %r)"
                    % (idx, coverage),
                )
            for scenario, verdict in coverage.items():
                declared.setdefault(scenario, []).append(verdict)
        incomplete = []
        for scenario in required_scenarios:
            verdicts = declared.get(scenario)
            if not verdicts:
                incomplete.append("%s=NOT_DECLARED" % (scenario,))
            elif any(v != "PASS" for v in verdicts):
                incomplete.append("%s=%s" % (scenario, "/".join(str(v) for v in verdicts)))
        if incomplete:
            raise ValidationError(
                "SCENARIO_COVERAGE_INCOMPLETE",
                "claim %s declares %d required scenario(s) without PASS evidence: %s"
                % (claim.get("claim_id"), len(incomplete), ", ".join(incomplete)),
            )


def validate_cancelled_claim(claim):
    if claim.get("state") == "Cancelled":
        if not claim.get("superseded_by"):
            raise ValidationError(
                "MISSING_SUPERSEDED_BY",
                "cancelled claim must declare superseded_by",
            )


def _validate_artifact_commit_lineage(artifact_commit, expected_commit, lineage):
    """校验"产物提交是 HEAD 的祖先、且二者之差仅限 claims/"这一血缘证明。

    证明由调用方（有 git 的那一层）算出；本函数只做形状与自洽性检查。
    """
    if not isinstance(lineage, dict):
        raise ValidationError("COMMIT_LINEAGE_INVALID", "artifact lineage proof must be an object")
    if lineage.get("head_commit") != expected_commit:
        raise ValidationError(
            "COMMIT_LINEAGE_INVALID",
            "lineage proof was computed against %r, not the expected commit %r"
            % (lineage.get("head_commit"), expected_commit),
        )
    if not FULL_COMMIT_PATTERN.match(str(artifact_commit or "")):
        raise ValidationError(
            "COMMIT_MISMATCH",
            "artifact_commit must be a full lowercase SHA-1 (%r)" % (artifact_commit,),
        )
    if lineage.get("artifact_commit") != artifact_commit:
        raise ValidationError("COMMIT_LINEAGE_INVALID", "lineage proof covers a different artifact_commit")
    if lineage.get("is_ancestor") is not True:
        raise ValidationError("COMMIT_LINEAGE_INVALID", "artifact_commit is not an ancestor of HEAD")
    if lineage.get("claims_only_delta") is not True:
        raise ValidationError(
            "COMMIT_LINEAGE_INVALID",
            "changes between artifact_commit and HEAD are not confined to the claims directory: %s"
            % ", ".join(lineage.get("offending_paths") or []),
        )
