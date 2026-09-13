"""契约：变异校验器必须**逐字节**恢复它改过的文件。

为什么单独钉这一条
------------------
2026-09-13 实测踩到：变异脚本用 `Path.read_text()` / `write_text()` 做「读→改→写回」，
而本仓 `core.autocrlf=true`（`git ls-files --eol` 显示 i/lf、w/crlf），
`write_text` 会把 LF 翻译成 CRLF ⇒ **原本 LF 的文件被静默改成 CRLF**，
hash 校验失败、脚本在第一个变异后就中止，**一个结果都没拿到**。

更危险的是这类损伤**看不见**：`git diff` 为空（autocrlf 归一化后无差异），
只有字节数会露出马脚（16000 → 16342，差值 342 = 行数）。
所以必须有契约把它钉死：改文件的工具**不许**依赖会做换行翻译的 API。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "scripts" / "mutation-check-contracts.py"


def test_mutation_harness_is_tracked() -> None:
    assert HARNESS.is_file(), "缺少 scripts/mutation-check-contracts.py"
    proc = subprocess.run(["git", "ls-files", "--error-unmatch",
                           "scripts/mutation-check-contracts.py"],
                          cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, "变异校验器未被 git 跟踪 ⇒ 别人无法复现"


def test_mutation_harness_restores_bytes_not_text() -> None:
    """恢复必须走字节：出现 `write_text(` 做回写就是回归。"""
    src = HARNESS.read_text(encoding="utf-8")
    assert "path.write_bytes(raw)" in src, "必须逐字节写回原始内容"
    assert "path.read_bytes() != raw" in src, "必须逐字节校验已还原"
    assert "path.write_text(" not in src, (
        "不得用 write_text 回写被测文件：core.autocrlf=true 下它会把 LF 翻成 CRLF，"
        "造成不可见的字节漂移（实测 main.rs 16000→16342）"
    )
    assert "read_text(" not in src.split("def _apply_mutation", 1)[1], (
        "变异施加路径同样必须走字节读取"
    )
