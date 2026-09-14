"""契约：`scripts/check-release-blockers.py` 必须**委托**给权威门禁，且不能自己编判定。

为什么要有这条
--------------
发布拦路项报告是给人看的决策依据。如果它**自己重写一遍门禁规则**，两份实现必然漂移，
而漂移出来的"就绪"是最危险的一类错误：**看起来放行、实则没验**。
所以本契约做两件独立的事：

  1. **行为**：用 tmp fixture 喂入不同状态的声明，断言报告的判定**随声明状态改变** ——
     Verified ⇒ 不再出现 `NOT_VERIFIED`；EvidencePending ⇒ 两个 P0 都必须被点名为阻塞。
     （只断言"文件存在"是假保护：脚本把判定写死成 fail 或写成通过都能过。）
  2. **结构**：断言它调用 `release_governance.verify.verify_claims`，而不是自造规则。

注意：`detect_worktree_clean` 检查的是**真实仓库**，测试环境必然是脏的 ⇒ 这里只断言
"声明相关的阻塞消失"，不断言整体 verdict=pass（那需要干净工作区，属于另一件事）。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check-release-blockers.py"

COMMIT = "a" * 40
ARTIFACT_SHA = "sha256:" + "b" * 64


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, states: dict[str, str]) -> tuple[Path, Path, Path, Path]:
    """造 policy / claims / command-lock / artifact 四件套，返回路径。"""
    evidence = tmp_path / "field-evidence.log"
    evidence.write_text("现场取证记录", encoding="utf-8")
    artifact = tmp_path / "candidate.bin"
    artifact.write_bytes(b"release-candidate")

    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    for claim_id, state in states.items():
        claim = {
            "claim_id": claim_id,
            "state": state,
            "owner": "impl-team",
            "reviewer": "independent-qa",
            "target": {"artifact_commit": COMMIT, "artifact_sha256": ARTIFACT_SHA},
            "evidence": [] if state != "Verified" else [{
                "kind": "android-field" if "android" in claim_id else "windows-field",
                "collected_at": "2026-09-14T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "path": str(evidence),
                "raw_sha256": _sha256_file(evidence),
            }],
            "attempts": [],
        }
        (claims_dir / f"{claim_id}.json").write_text(
            json.dumps(claim, ensure_ascii=False), encoding="utf-8")

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1,
        "required_claim_ids": list(states),
        "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
        "max_attempts_same_fingerprint": 2,
        "max_evidence_age_hours": 72,
        "required_checks": [],
        "release_channel": "production",
    }), encoding="utf-8")

    lock = tmp_path / "command-lock.json"
    lock.write_text(json.dumps({"schema_version": 1, "checks": []}), encoding="utf-8")
    return policy, claims_dir, lock, artifact


def _run(policy: Path, claims: Path, lock: Path, artifact: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--policy", str(policy), "--claims", str(claims),
         "--command-lock", str(lock), "--commit", COMMIT,
         "--artifact", str(artifact), "--json"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_script_is_tracked() -> None:
    assert SCRIPT.is_file(), "缺少 scripts/check-release-blockers.py"
    proc = subprocess.run(["git", "ls-files", "--error-unmatch",
                           "scripts/check-release-blockers.py"],
                          cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, "拦路项检查器未被 git 跟踪 ⇒ 别人无法复现"


def test_delegates_to_the_authoritative_gate() -> None:
    """不得自造门禁规则 —— 必须调用 verify_claims。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "verify_claims" in src, "必须调用权威判定，而不是自己实现一套"
    assert "from scripts.release_governance.verify import" in src, "必须直接复用治理层"
    # 反例守卫：不许自己写 verdict 判定（那正是漂移的来源）
    assert "DIRTY_WORKTREE" not in src.split("verify_claims(", 1)[0], \
        "判定逻辑必须在治理层，不在本脚本的前置代码里"


def test_verified_claims_clear_the_claim_blockers(tmp_path: Path) -> None:
    """两个 P0 都是 Verified ⇒ 不应再出现 NOT_VERIFIED（判定确实随输入变化）。"""
    policy, claims, lock, artifact = _fixture(
        tmp_path, {"windows-popup-free": "Verified", "android-duplex-audio": "Verified"})
    code, payload = _run(policy, claims, lock, artifact)
    codes = [e.get("code") for e in payload["errors"]]
    assert "NOT_VERIFIED" not in codes, f"Verified 之后不该再报 NOT_VERIFIED：{payload['errors']}"
    assert "MISSING_REQUIRED_CLAIM" not in codes
    assert code in (0, 1), f"退出码只应是 0/1，实测 {code}"


def test_pending_claims_are_named_as_blockers(tmp_path: Path) -> None:
    """EvidencePending ⇒ 两个 P0 都必须被点名，且退出码为阻塞。"""
    policy, claims, lock, artifact = _fixture(
        tmp_path, {"windows-popup-free": "EvidencePending", "android-duplex-audio": "EvidencePending"})
    code, payload = _run(policy, claims, lock, artifact)
    assert code == 1, f"有阻塞时退出码必须为 1，实测 {code}"
    blocked = {e.get("claim_id") for e in payload["errors"] if e.get("code") == "NOT_VERIFIED"}
    assert blocked == {"windows-popup-free", "android-duplex-audio"}, payload["errors"]


def test_missing_claim_file_is_a_blocker_not_a_pass(tmp_path: Path) -> None:
    """policy 要求但 claims/ 里没有该文件 ⇒ 必须报 MISSING_REQUIRED_CLAIM。"""
    policy, claims, lock, artifact = _fixture(tmp_path, {"windows-popup-free": "Verified"})
    payload_policy = json.loads(policy.read_text(encoding="utf-8"))
    payload_policy["required_claim_ids"] = ["windows-popup-free", "android-duplex-audio"]
    policy.write_text(json.dumps(payload_policy), encoding="utf-8")
    code, payload = _run(policy, claims, lock, artifact)
    assert code == 1
    assert any(e.get("code") == "MISSING_REQUIRED_CLAIM" for e in payload["errors"]), payload
