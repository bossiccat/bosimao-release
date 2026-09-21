"""打印「商业发布到底卡在哪」的精确清单 —— 一条命令，而不是靠人复述。

设计要点（都是刻意的）
----------------------
1. **判定委托给 `release_governance.verify.verify_claims`，本脚本不重写门禁规则。**
   两份实现必然漂移，而漂移出来的"GO"是最危险的一类错误（看起来放行、实则没验）。
   本脚本只做两件事：调用权威判定 + 把错误枚举成**可执行的**下一步。
2. **不确定就说不确定。** 锁定检查的封存结果（HMAC）需要
   `--evidence-root` + `--release-id` + CI 的 HMAC 密钥，本地拿不到 ⇒ 明确标注
   "本地不验证"，绝不用"通过"或"跳过"冒充。
3. **退出码有意义**：0=就绪，1=有阻塞，2=输入不可用（配置缺失等）。
   fail-closed：输入读不到一律 2，绝不因为读不到就当作"没阻塞"。

用法：
    ./.venv/Scripts/python.exe scripts/check-release-blockers.py
    ./.venv/Scripts/python.exe scripts/check-release-blockers.py --artifact outputs/pkg.exe --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_governance.verify import (  # noqa: E402
    detect_worktree_clean,
    verify_claims,
)

EXIT_READY, EXIT_BLOCKED, EXIT_UNUSABLE = 0, 1, 2


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_status() -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    return [l for l in out.splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="governance/release-policy.json")
    ap.add_argument("--claims", default="governance/claims")
    ap.add_argument("--command-lock", default="governance/command-lock.json")
    ap.add_argument("--commit", default="")
    ap.add_argument("--artifact", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        policy = _load(ROOT / args.policy)
        claims_dir = ROOT / args.claims
        lock = _load(ROOT / args.command_lock)
    except (OSError, ValueError) as exc:
        print(f"输入不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    commit = args.commit or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
        text=True).stdout.strip()
    if not commit:
        print("输入不可用：拿不到 HEAD commit", file=sys.stderr)
        return EXIT_UNUSABLE

    artifact_sha = ""
    artifact_note = "未提供 --artifact ⇒ 声明无法绑定产物，验证必然失败（这是正确的 fail-closed）"
    if args.artifact:
        p = ROOT / args.artifact
        if not p.is_file():
            print(f"输入不可用：产物不存在 {p}", file=sys.stderr)
            return EXIT_UNUSABLE
        artifact_sha = _sha256(p)
        artifact_note = f"{args.artifact} -> {artifact_sha[:27]}…"
    artifact_sha = artifact_sha or "sha256:" + "0" * 64   # 占位：故意不可能匹配

    worktree_clean = detect_worktree_clean(ROOT)
    result = verify_claims(
        policy=policy, claims_dir=claims_dir, expected_commit=commit,
        expected_artifact_sha256=artifact_sha,
        now_utc=datetime.now(timezone.utc), worktree_clean=worktree_clean,
        repo_root=ROOT,
    )
    verdict = result["verdict"]
    errors = result["errors"]

    # ---- 枚举：把 errors 变成可执行动作（不改判定，只做解释） ----
    required = policy.get("required_claim_ids", [])
    claims = {}
    for path in sorted(claims_dir.glob("*.json")) if claims_dir.is_dir() else []:
        try:
            c = _load(path)
        except (OSError, ValueError):
            continue
        if isinstance(c, dict) and c.get("claim_id"):
            claims[c["claim_id"]] = c

    dirty = _git_status()
    untracked = [l for l in dirty if l.startswith("??")]
    modified = [l for l in dirty if not l.startswith("??")]
    checks = [c for c in lock.get("checks", []) if isinstance(c, dict)]

    if args.json:
        print(json.dumps({
            "verdict": verdict, "commit": commit, "artifact": artifact_sha,
            "worktree_clean": worktree_clean, "errors": errors,
            "required_claims": required, "required_checks": policy.get("required_checks", []),
        }, ensure_ascii=False, indent=2))
        return EXIT_READY if verdict == "pass" else EXIT_BLOCKED

    print("== 商业发布拦路项 ==")
    print(f"事实来源: scripts/release_governance/verify.verify_claims（本脚本不重写门禁规则）")
    print(f"commit  : {commit[:12]}   产物: {artifact_note}")
    print(f"判定    : {'✅ 就绪' if verdict == 'pass' else f'❌ 未就绪（{len(errors)} 项阻塞）'}")
    print()

    print("[1] P0 声明（policy.required_claim_ids）")
    for cid in required:
        c = claims.get(cid)
        if c is None:
            print(f"  • {cid:26} **缺失** ← 阻塞（policy 要求，但 claims/ 里没有这个文件）")
            continue
        state = c.get("state", "?")
        ev = c.get("evidence") or []
        mark = "✓ 已 Verified" if state == "Verified" else f"← 阻塞：{state} ≠ Verified"
        print(f"  • {cid:26} {state:16} {mark}")
        print(f"      所有者={c.get('owner','?')}  复核人={c.get('reviewer','?')}  证据 {len(ev)} 份")
        for e in ev:
            print(f"      - kind={e.get('kind')} expires_at={e.get('expires_at')} "
                  f"hash={str(e.get('raw_sha256') or e.get('result_sha256'))[:20]}…")
    print()

    print("[2] 工作区（release verification 要求干净）")
    if worktree_clean:
        print("  ✓ 干净")
    else:
        print(f"  • 脏：{len(untracked)} 项未跟踪 / {len(modified)} 项已改 ← 阻塞（DIRTY_WORKTREE）")
        for l in (untracked + modified)[:5]:
            print(f"      {l[:90]}")
        if len(dirty) > 5:
            print(f"      …（共 {len(dirty)} 项）")
    print()

    print("[3] 锁定检查（policy.required_checks，需命令锁里已声明）")
    lock_ids = {c.get("id") for c in checks}
    for cid in policy.get("required_checks", []):
        if cid in lock_ids:
            c = next(x for x in checks if x.get("id") == cid)
            print(f"  • {cid:20} 已声明  expected_exit={c.get('expected_exit')}  "
                  f"argv={' '.join(c.get('argv', []))[:70]}")
        else:
            print(f"  • {cid:20} **命令锁里未声明** ← 阻塞")
    print("  ⚠️ 封存结果（HMAC）需要 --evidence-root + --release-id + CI 密钥 —— "
          "**本地不验证**，此处不做真假判定。")
    print()

    if verdict != "pass":
        print("下一步（按顺序）：")
        n = 0
        for e in errors:
            code = e.get("code") if isinstance(e, dict) else str(e)
            if code == "NOT_VERIFIED":
                cid = e.get("claim_id")
                # 证据为空时没有 kind 可读 ⇒ 回落到约定值（policy.allowed_evidence_kinds 里的命名）
                kind = next((x.get("kind") for x in
                             (claims.get(cid, {}).get("evidence") or [])), "")
                if not kind:
                    kind = "android-field" if "android" in str(cid) else "windows-field"
                where = "**需要真机**（Android 现场）" if kind == "android-field" \
                    else "需要 Windows 现场取证"
                print(f"  {n+1}. 把 `{cid}` 变成 Verified：补 kind={kind} 的证据（{where}）；"
                      f"把证据文件路径写进 claim 的 evidence[].path、"
                      f"raw_sha256 填实际哈希、expires_at ≤ 现在+72h、reviewer ≠ owner")
                n += 1
            elif code == "DIRTY_WORKTREE":
                print(f"  {n+1}. 清干净工作区（{len(dirty)} 项，主要是未跟踪的构建/备份/测试残留）")
                n += 1
            else:
                print(f"  {n+1}. 处理 {code}：{e.get('message') if isinstance(e, dict) else ''}")
                n += 1
        print("  最后：跑 scripts/release-preflight.py verify 确认判定为 pass —— ")
        print("        注意 verify 需要 6 个必填参数（--policy/--claims/--command-lock/")
        print("        --repo-root/--artifact-path/--evidence-root），完整写法见")
        print("        docs/governance/release-harness.md 的「运行命令」节。")
    return EXIT_READY if verdict == "pass" else EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
