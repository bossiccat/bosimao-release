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


def _size(path: str) -> tuple[int, int, int]:
    """返回 (表观大小, 独占大小, 硬链接文件数/字节)。

    ⚠️ 为什么必须过滤 `st_nlink > 1`（2026-09-14 血的教训）：
    表观大小用 `getsize` 累加会**重复计数被硬链接共享的数据块**，cargo target 尤其严重
    （实测活跃 `pet-ui/src-tauri/target` 里 1220 个文件 / 2181 MB 是 nlink>1）。
    据此估"可回收"会**大幅虚高** —— 实测删掉 6 个目录（表观 4.6 GB）后，
    C 盘可用空间**不升反降 1.34 GB**，因为那些块仍被活跃 target 持有。
    ⇒ 只有"独占大小"才是真正能释放的量。
    """
    p = ROOT / path
    if not p.exists():
        return 0, 0, 0
    apparent = exclusive = 0
    shared_n = shared_b = 0
    if p.is_dir():
        for root, _d, files in os.walk(p):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                apparent += st.st_size
                if st.st_nlink > 1:
                    shared_n += 1
                    shared_b += st.st_size
                else:
                    exclusive += st.st_size
    else:
        try:
            st = p.stat()
        except OSError:
            return 0, 0, 0
        apparent = exclusive = st.st_size
        if st.st_nlink > 1:
            shared_n, shared_b = 1, st.st_size
    return apparent, exclusive, (shared_n, shared_b)  # type: ignore[return-value]


def main() -> int:
    raw = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    untracked = [l[3:].rstrip() for l in raw.splitlines() if l.startswith("??")]
    raw_rows = []
    for path in untracked:
        apparent, exclusive, shared = _size(path)
        raw_rows.append((apparent, exclusive, shared[0], shared[1], path, _classify(path)))
    rows = sorted(raw_rows, reverse=True)

    groups: dict[str, list[tuple[int, int, int, int, str]]] = {"KEEP": [], "CLEAN": [], "ASK": []}
    for apparent, exclusive, shn, shb, path, tag in rows:
        groups[tag].append((apparent, exclusive, shn, shb, path))

    def mb(n: int) -> str:
        return f"{n / 1024 / 1024:.1f} MB"

    tot_apparent = sum(r[0] for r in rows)
    tot_exclusive = sum(r[1] for r in rows)
    tot_shared_b = sum(r[3] for r in rows)

    lines = [
        "# 未跟踪条目盘点（发布拦路项 · 只读清单）",
        "",
        f"共 **{len(rows)}** 条：表观 **{mb(tot_apparent)}**，"
        f"其中**独占 {mb(tot_exclusive)}**、硬链接共享 **{mb(tot_shared_b)}**。",
        "",
        "> ⚠️ **估算可回收量只看「独占」列。** 表观大小会把被硬链接共享的数据块重复计数"
        "（cargo target 尤其严重）。2026-09-14 实测：删掉表观 4.6 GB 的 6 个目录后，"
        "C 盘可用空间**不升反降 1.34 GB** —— 那些块仍被活跃 target 持有。",
        "",
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
                  f"共 {len(items)} 条：表观 {mb(sum(i[0] for i in items))}，"
                  f"独占 {mb(sum(i[1] for i in items))}", "",
                  "| 表观 | 独占 | 硬链接 | 路径 |", "|---|---|---|---|"]
        lines += [f"| {mb(a)} | {mb(e)} | {n} 个/{mb(b)} | `{p}` |"
                  for a, e, n, b, p in items]
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
        items = groups[tag]
        print(f"  {tag:6} {len(items):3} 条  表观 {mb(sum(i[0] for i in items))}"
              f"  独占 {mb(sum(i[1] for i in items))}")
    print(f"\n表观合计 {mb(tot_apparent)} / 独占合计 {mb(tot_exclusive)} / {len(rows)} 条")
    print(f"清单已写入: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
