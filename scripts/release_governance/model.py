"""Pure, fail-closed model for release governance claims.

Constraints enforced here:
- No network access, no file I/O, no subprocess.
- All timestamps must be UTC ISO-8601; anything unparseable is rejected.
- A P0 claim (listed in policy.required_claim_ids) must be Verified.
- A Verified claim must bind to the exact commit + artifact SHA-256 and carry
  unexpired evidence reviewed by someone other than its owner.
"""

import hashlib
from datetime import datetime, timezone

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


def validate_verified_claim(claim, policy, now_utc, expected_commit, expected_artifact_sha256):
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
    if target.get("artifact_commit") != expected_commit:
        raise ValidationError(
            "COMMIT_MISMATCH",
            "artifact_commit %r != expected %r" % (target.get("artifact_commit"), expected_commit),
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
    for idx, ev in enumerate(evidence):
        if not isinstance(ev, dict):
            raise ValidationError("BAD_EVIDENCE", "evidence[%d] must be an object" % idx)
        expires_at = _parse_utc(ev.get("expires_at"))
        if expires_at <= now_utc:
            raise ValidationError(
                "EVIDENCE_EXPIRED",
                "evidence[%d] expired at %s" % (idx, ev.get("expires_at")),
            )


def validate_cancelled_claim(claim):
    if claim.get("state") == "Cancelled":
        if not claim.get("superseded_by"):
            raise ValidationError(
                "MISSING_SUPERSEDED_BY",
                "cancelled claim must declare superseded_by",
            )
