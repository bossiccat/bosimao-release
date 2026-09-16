"""契约：**任何已跟踪文件都不许被 .gitignore 命中**。

为什么必须有这条（2026-09-16 实测发现）
---------------------------------------
`.gitignore` 与实际状态出现了自相矛盾 —— **7 个已入库的文件被忽略规则掩盖**：

| 规则 | 被掩盖的已跟踪文件 |
|---|---|
| `qa-*/`（未锚定） | `sidecar-smoke/qa-phone/{index.html,main.js,renderer.js}` —— 冒烟测试的**源码** |
| `outputs/` | 4 份设备验收/指标报告 `*.md` —— **内容**，不是草稿 |

危害不是"看起来乱"，而是**具体且会咬人**：
- 在该目录里**新增**文件会被静默忽略 ⇒ 想入库必须知道要用 `git add -f`，否则
  "我明明加了文件却没进提交"；
- 规则与事实矛盾会让后续所有基于 `.gitignore` 的判断（含清理脚本、CI 打包范围）不可靠。

判据用 git 自己的口径：`git ls-files -i -c --exclude-standard`
= "已缓存在索引里、却匹配忽略规则的文件"。**它必须为空。**
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_no_tracked_file_is_matched_by_gitignore() -> None:
    proc = subprocess.run(["git", "ls-files", "-i", "-c", "--exclude-standard"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"git ls-files 失败：{proc.stderr[:200]}"
    offenders = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    assert not offenders, (
        "以下文件**已经在版本控制里**，却被 .gitignore 命中 —— 规则与实际自相矛盾。\n"
        "修法二选一：把规则锚定/收窄到不再命中它；或把文件移到不被忽略的目录。\n  - "
        + "\n  - ".join(offenders)
    )


def test_qa_temp_dirs_rule_stays_root_anchored() -> None:
    """专项守卫：`qa-*/` 必须锚定到根。

    不锚定会吞掉 `sidecar-smoke/qa-phone/`（3 个已入库源码）；而根级确有 14 个
    `qa-task*/qa-cargo-probe*` 一次性目录，锚定后原意不变。
    """
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    rule_lines = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    assert "/qa-*/" in rule_lines, "qa-* 目录规则必须锚定到根（写成 /qa-*/）"
    assert "qa-*/" not in rule_lines, "未锚定的 qa-*/ 会吞掉 sidecar-smoke/qa-phone/ 里的源码"


def test_outputs_is_scratch_only_no_tracked_reports() -> None:
    """`outputs/` 是草稿区。若它下面出现已跟踪文件，说明又有内容型文件被放进了被忽略目录。"""
    proc = subprocess.run(["git", "ls-files", "outputs/"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    tracked = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    assert not tracked, (
        "outputs/ 被 .gitignore 整目录忽略，不应有已跟踪文件（内容型报告请放 docs/reports/）：\n  - "
        + "\n  - ".join(tracked)
    )
