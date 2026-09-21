"""记录一条发布声明的证据，**写盘前用真实校验器验证**。

为什么需要它
------------
P0 声明要变成 Verified，必须同时满足一堆机械条件（`validate_verified_claim`
+ `_validate_evidence_hashes`）：证据文件存在且哈希对得上、`expires_at` 未过期、
`reviewer != owner`、`target` 绑定正确的 commit 与产物 sha。手改 JSON 时最容易错的
就是**哈希**和**时效窗口**两项，而错了的后果是"门禁悄悄不通过"或"看似 Verified 实则无效"。

本工具只做两件事：把给定的证据文件**算成正确的哈希**、
**按采集时刻（`--collected-at`，缺省取当前时刻）算出合法的时效窗口**，
然后**用治理层自己的校验器验证**，不通过就拒绝写盘。
`expires_at` 一律由 `collected_at + policy.max_evidence_age_hours` 导出 ——
**不由写入时刻导出**，否则一份旧证据会白拿一段虚假有效期。
**它不评判证据内容** —— 见下面的诚实边界。

诚实边界（必须写清，别把它当审批工具）
--------------------------------------
- 它**不核实证据的真伪**。给它一个写着 PASS 的文件，它照样能算哈希、写出合法声明。
- 真正的控制点是 **reviewer**：`reviewer != owner` 是策略要求，但"独立复核确实发生过"
  这件事只有人知道，工具证明不了。
- 所以：**用它减少机械错误，不要用它替代复核。**

用法：
    ./.venv/Scripts/python.exe scripts/record-release-evidence.py \
        --claim-id android-duplex-audio --kind android-field \
        --evidence outputs/field/android-duplex.log \
        --artifact outputs/candidate/bosimao-release-apk.tar.gz \
        --owner impl-team --reviewer independent-qa \
        --collected-at 2026-09-19T12:18:18Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_governance.model import (  # noqa: E402
    ValidationError,
    _parse_utc,
    validate_verified_claim,
)
from scripts.release_governance.verify import _validate_evidence_hashes  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_UNUSABLE = 0, 1, 2


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim-id", required=True)
    ap.add_argument("--kind", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--artifact", default="", help="产物文件路径（用于取 sha256）")
    ap.add_argument("--artifact-sha", default="", help="或直接给 sha256:<hex>")
    ap.add_argument("--commit", default="")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--collected-at", default="",
                    help="证据的**实际采集时刻**（RFC3339/UTC，如 2026-09-19T12:18:18Z）。"
                         "省略则取当前时刻。expires_at 由它 + policy.max_evidence_age_hours 导出。")
    ap.add_argument("--policy", default="governance/release-policy.json")
    ap.add_argument("--claims-dir", default="governance/claims")
    ap.add_argument("--allow-reverify", action="store_true",
                    help="允许覆盖一条已 Verified 的声明（默认拒绝）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        policy = json.loads((ROOT / args.policy).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"输入不可用：读 policy 失败 {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    # ---- 机械条件：先全部拦下来，绝不写一份"看起来对"的声明 ----
    if args.reviewer == args.owner:
        print("拒绝：reviewer 必须与 owner 不同（policy 要求独立复核）", file=sys.stderr)
        return EXIT_REFUSED
    allowed = policy.get("allowed_evidence_kinds", [])
    if allowed and args.kind not in allowed:
        print(f"拒绝：kind={args.kind!r} 不在 policy.allowed_evidence_kinds {allowed}", file=sys.stderr)
        return EXIT_REFUSED

    evidence = ROOT / args.evidence
    if not evidence.is_file():
        print(f"拒绝：证据文件不存在 {evidence}（本工具不会凭空造证据）", file=sys.stderr)
        return EXIT_REFUSED

    artifact_sha = args.artifact_sha.strip()
    if not artifact_sha:
        if not args.artifact:
            print("拒绝：必须给 --artifact 或 --artifact-sha（声明必须绑定产物）", file=sys.stderr)
            return EXIT_UNUSABLE
        artifact_path = ROOT / args.artifact
        if not artifact_path.is_file():
            print(f"拒绝：产物不存在 {artifact_path}", file=sys.stderr)
            return EXIT_REFUSED
        artifact_sha = _sha256_file(artifact_path)

    commit = args.commit or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
        text=True).stdout.strip()
    if not commit:
        print("输入不可用：拿不到 commit", file=sys.stderr)
        return EXIT_UNUSABLE

    claims_dir = ROOT / args.claims_dir
    target_claim = claims_dir / f"{args.claim_id}.json"
    # 既有声明要**合并**进新声明，不能整体覆盖：`risk`（在断言什么）、
    # `required_scenarios`（必须演示哪些场景）、`legacy_tasks` / `superseded_by`（溯源）、
    # `target.artifact`（绑定哪条产物链）、`attempts`（同一失败指纹的重试历史，
    # 见 verify._validate_attempt_circuit）都不是本工具该改写的字段。
    # 只整体覆盖会让声明"门禁变绿但语义消失"——审计者再也看不出原本要证明什么。
    existing = {}
    if target_claim.is_file():
        try:
            existing = json.loads(target_claim.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if existing.get("state") == "Verified" and not args.allow_reverify:
            print(f"拒绝：{args.claim_id} 已是 Verified；覆盖需显式 --allow-reverify",
                  file=sys.stderr)
            return EXIT_REFUSED

    now = datetime.now(timezone.utc)
    hours = int(policy.get("max_evidence_age_hours", 72))
    ts = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

    # 时效锚在**采集时刻**：证据是什么时候采的，就什么时候开始计 72h。
    # 之前这里用写入时刻 ⇒ 采于很久以前的证据只要今天被记录，就能白拿一段
    # 虚假有效期（实测多出约 41.9h），是一条"看起来在有效期内、其实早已过期"的假绿通道。
    if args.collected_at.strip():
        try:
            collected_at = _parse_utc(args.collected_at.strip())
        except ValidationError as exc:
            print(f"输入不可用：--collected-at 不可解析（{exc.message}）", file=sys.stderr)
            return EXIT_UNUSABLE
    else:
        collected_at = now

    # 从既有声明起手再覆写本工具负责的字段 —— 其余治理元数据原样留下。
    claim = dict(existing) if isinstance(existing, dict) else {}
    claim["claim_id"] = args.claim_id
    claim["state"] = "Verified"
    claim["owner"] = args.owner
    claim["reviewer"] = args.reviewer
    prior_target = claim.get("target")
    target = dict(prior_target) if isinstance(prior_target, dict) else {}
    target["artifact_commit"] = commit
    target["artifact_sha256"] = artifact_sha
    claim["target"] = target
    claim["evidence"] = [{
        "kind": args.kind,
        "collected_at": ts(collected_at),
        "expires_at": ts(collected_at + timedelta(hours=hours)),
        "path": args.evidence,
        "raw_sha256": _sha256_file(evidence),
    }]
    # attempts 已有就保留（电路判据依赖历史）；只有缺省时才补空列表。
    claim.setdefault("attempts", [])

    # ---- 写盘前用治理层自己的校验器验证（不是自己再写一遍规则） ----
    try:
        validate_verified_claim(claim, policy, now, commit, artifact_sha)
    except ValidationError as exc:
        print(f"拒绝：构造出的声明未通过治理层校验 {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REFUSED
    hash_errors = _validate_evidence_hashes(claim)
    if hash_errors:
        print(f"拒绝：证据哈希校验失败 {hash_errors}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"声明    : {args.claim_id} -> Verified")
    print(f"kind    : {args.kind}")
    print(f"证据    : {args.evidence}  {claim['evidence'][0]['raw_sha256'][:27]}…")
    print(f"产物    : {artifact_sha[:27]}…  @ commit {commit[:12]}")
    print(f"采集    : {claim['evidence'][0]['collected_at']}")
    print(f"有效期  : {claim['evidence'][0]['expires_at']}（= 采集时刻 + policy {hours}h）")
    print(f"复核    : owner={args.owner} reviewer={args.reviewer}")
    if args.dry_run:
        print("\n--dry-run：未写盘。")
        return EXIT_OK
    claims_dir.mkdir(parents=True, exist_ok=True)
    target_claim.write_text(json.dumps(claim, ensure_ascii=False, indent=2), encoding="utf-8")
    # claims-dir 允许在仓库外（测试/临时目录）⇒ relative_to 可能抛 ValueError。
    # 之前这里直接抛，导致「文件已写入但进程非零退出」—— 状态与退出码不一致。
    try:
        shown = target_claim.relative_to(ROOT)
    except ValueError:
        shown = target_claim
    print(f"\n已写入 {shown}")
    print("⚠️ 本工具只保证**机械条件**成立；证据内容是否真实由 reviewer 负责。")
    print("   下一步：scripts/check-release-blockers.py 复核，再跑 release-preflight.py verify。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
