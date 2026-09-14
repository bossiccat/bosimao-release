"""契约：未跟踪条目盘点工具**只读**，且必须明确分类。

为什么
-----
运行清理前先清点，是这个仓库的硬要求（历史上有过不可逆的数据损失事故）。
盘点工具一旦"顺手删一下"，就会把 `.git.backup-*`（git 事故恢复材料）、
`jax-backend.exe.bak-pre-swap-*`（生产回滚备份）、`pet-ui/src-tauri/binaries/`
（打包流程引用，1.7GB）这类**必须保留**的东西一起清掉。
所以本契约钉两件事：① 工具不删除任何东西；② 必须保留 / 可清理 的分类同时存在，
不允许退化成"全部可删"。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "inventory-untracked.py"


def test_script_is_tracked() -> None:
    assert SCRIPT.is_file()
    proc = subprocess.run(["git", "ls-files", "--error-unmatch",
                           "scripts/inventory-untracked.py"],
                          cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, "盘点工具未被 git 跟踪"


def test_inventory_never_deletes() -> None:
    """只读：出现任何删除调用即为回归。"""
    src = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(", "os.rmdir(",
                      "rm -rf", "os.rename(", "shutil.move("):
        assert forbidden not in src, f"盘点工具不得执行删除/移动：{forbidden}"


def test_inventory_keeps_a_protected_class() -> None:
    """必须保留类必须存在且覆盖关键项 —— 防止分类漂移成"全都可清"。"""
    src = SCRIPT.read_text(encoding="utf-8")
    for protected in ("pet-ui/src-tauri/binaries/", "jax-backend.exe.bak-pre-swap-",
                      ".git.backup-", "certs/client.crt"):
        assert protected in src, f"必须保留清单里缺少 {protected}"
