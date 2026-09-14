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
