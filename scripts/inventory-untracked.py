"""未跟踪条目盘点 —— 分类清单（**只读，不删除任何东西**）。

为什么需要它
------------
`verify_claims` 要求 `git status --porcelain` **输出为空**，所以未跟踪残留**自己就能拦住发布**。
但在动它们之前必须分清三类：**必须保留的**、**可安全清理的生成物/缓存**、**需要人判断的**。
误删 `.git.backup-*`（git 事故恢复材料）、`jax-backend.exe.bak-pre-swap-*`（生产回滚备份）、
`pet-ui/src-tauri/binaries/`（打包流程引用）都会造成不可逆损失。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 必须保留：被流程引用，或承载回滚/恢复能力
KEEP_PATTERNS = (
    "pet-ui/src-tauri/binaries/",
    "jax-backend.exe.bak-pre-swap-",
    ".git.backup-",
    ".git.disaster-",
    "certs/client.crt",
    ".idsig",
    "specs/",
)
# 可安全清理：构建产物 / 缓存 / 一次性探针输出
CLEAN_PATTERNS = (
    ".pytest-",
    "target-check/", "target-fresh-", "target-sidecar-", "target-rp07-", "target-fresh-o018/",
    ".cargo-home-online/", "WindowsTempcargo-sidecar-test/", "CON",
    "sidecar/_nm-quarantine-", "sidecar/_trash-", "sidecar/ca-tmp.crt",
    "backend/packaging/build-", "backend/packaging/dist-",
    ".t58_", ".t59_", "_jaxprocs", "_jrt_procs", "_defender_probe", "_disk_probe2",
    "_kill26636", "_o019_backend_tree", "_postclean_check", "_proc_probe", "_wlan_probe",
    "backend_ls.txt", "deps_check.txt", "dirs_check.txt", "dup_check.txt",
    "final_diff.txt", "git_log_check", "init_check.txt", "linecount.txt", "main_dirs.txt",
    "b2_unstaged.txt", "red_claim_returning.txt", "red_out.txt", "red_results.xml",
    "full_regression_claim_returning.txt", "termination-test-output.txt",
    "wt_test", "analysis_result.json", "overview-", "task20-source-tar/", ".ece.json",
    "pet-ui/.._o020_", "pet-ui/src-tauri/_cargo_", "pet-ui/src-tauri/_walk_probe.py",
    "qa_r3_verify_generation.js", "_probe_lock_unlink.js", "_unlink_probe_require.js",
)


def _classify(path: str) -> str:
    if any(k in path for k in KEEP_PATTERNS):
        return "KEEP"
    if any(k in path for k in CLEAN_PATTERNS):
        return "CLEAN"
    return "ASK"


def _size(path: str) -> int:
    p = ROOT / path
    if p.is_dir():
        total = 0
        for root, _d, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    try:
        return p.stat().st_size
    except OSError:
        return 0


def main() -> int:
    raw = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    untracked = [l[3:].rstrip() for l in raw.splitlines() if l.startswith("??")]
    rows = sorted(((_size(p), p, _classify(p)) for p in untracked), reverse=True)

    groups: dict[str, list[tuple[int, str]]] = {"KEEP": [], "CLEAN": [], "ASK": []}
    for size, path, tag in rows:
        groups[tag].append((size, path))

    def mb(n: int) -> str:
        return f"{n / 1024 / 1024:.1f} MB"

    lines = [
        "# 未跟踪条目盘点（发布拦路项 · 只读清单）",
        "",
        f"共 **{len(rows)}** 条，合计 **{mb(sum(s for s, _, _ in rows))}**。",
        "**本清单只做分类，没有删除任何文件。**",
        "",
        "> 背景：`verify_claims` 要求 `git status --porcelain` 输出为空，",
        "> 所以这些残留**自身就能拦住商业发布**（`DIRTY_WORKTREE`）。",
        "",
    ]
    for tag, title, note in (
        ("KEEP", "① 必须保留（被流程引用 / 承载回滚与恢复能力）",
         "误删不可逆：打包运行时、生产回滚备份、git 事故恢复材料、发布签名产物、设计规格。"),
        ("CLEAN", "② 可安全清理（构建产物 / 缓存 / 一次性探针输出）",
         "生成物或一次性诊断输出，删掉只是丢历史噪音；建议先移入回收站而非直接删。"),
        ("ASK", "③ 需要你判断", "我无法从仓库判断其价值，列在这里等你确认。"),
    ):
        items = groups[tag]
        lines += [f"## {title}", "", note, "",
                  f"共 {len(items)} 条，合计 {mb(sum(s for s, _ in items))}", "",
                  "| 体积 | 路径 |", "|---|---|"]
        lines += [f"| {mb(s)} | `{p}` |" for s, p in items]
        lines.append("")

    lines += [
        "## 建议的处理顺序",
        "",
        "1. 先处理 ②（体积大、纯生成物、零信息损失）——预计可让工作区变干净。",
        "2. ① 中的 `.git.backup-*` / `.git.disaster-*` 建议移出仓库（移到仓库外的备份目录）"
        "而不是删除：它们是历次 ref 事故的恢复材料，且**留在仓库里就会一直被 git 视为未跟踪**。",
        "3. `jax-backend.exe.bak-pre-swap-*`（6 份生产回滚备份）待确认当前生产版本后，"
        "只保留最近 1–2 份。",
        "4. ③ 逐条确认后归入 ① 或 ②。",
        "5. 全部处理完跑 `scripts/check-release-blockers.py` 复核 `DIRTY_WORKTREE` 是否消失。",
        "",
        "**注意**：即使清干净，发布仍被两个 P0 声明（`windows-popup-free` / `android-duplex-audio`）"
        "的 `EvidencePending` 拦住 —— 那需要真机取证，不是清理能解决的。",
    ]

    out = ROOT / "outputs" / "untracked-inventory.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    for tag in ("KEEP", "CLEAN", "ASK"):
        n = len(groups[tag])
        s = sum(x for x, _ in groups[tag])
        print(f"  {tag:6} {n:3} 条  {mb(s)}")
    print(f"\n总占用 {mb(sum(s for s, _, _ in rows))} / {len(rows)} 条")
    print(f"清单已写入: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
