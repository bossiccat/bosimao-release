"""契约：工作区里**不许残留变异态**。

为什么必须有这条（2026-09-15 实测事故）
---------------------------------------
`scripts/mutation-check-contracts.py` 在 `finally` 里恢复文件，但**进程被杀时 `finally` 不执行**。
实测一次被中断的运行留下了 3 个变异态文件：

| 文件 | 残留形态 | 后果 |
|---|---|---|
| `backend/rtc_bridge/bounded_audio_queue.py` | 帧龄阈值被放大 1000 倍（`> limit_ms * 1000`） | 下行几乎不再按帧龄丢帧 |
| `backend/rtc_bridge/session.py` | 打断冲刷被换成 `pass` | 打断不再通知 sidecar pacer |
| `pet-ui/src-tauri/src/main.rs` | 控制面指回已退役的 `jax-backend` | 桌面端连错控制面 |

同时留下陈旧 `.git/index.lock`，导致后续 `git add` 直接失败。

这次是**契约测试把残留暴露出来的**（6 个用例变红）。但那是运气：
如果残留的变异恰好落在**测试覆盖不到**的地方，它就会**静默进产物**。
所以需要一条**与变异覆盖无关**的兜底：直接扫工作区里有没有变异标记。

排除项：`scripts/mutation-check-contracts.py` 字面上就含 "MUTATED"（那是它的变异内容），
`backend/tests/contract/` 里的测试文件也可能引用这些字面量。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# 只看会被变异的源码区域；排除"字面上就该含 MUTATED"的文件
PATHS = ["backend", "pet-ui", "cloudapi", "scripts"]
EXCLUDES = [":(exclude)scripts/mutation-check-contracts.py",
            ":(exclude)backend/tests/contract"]


def test_working_tree_has_no_mutation_leftovers() -> None:
    proc = subprocess.run(
        ["git", "grep", "-l", "MUTATED", "--", *PATHS, *EXCLUDES],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    leftovers = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    assert not leftovers, (
        "工作区存在变异残留（上一次变异运行被中断）。"
        "先 `git checkout -- <文件>` 还原再继续，否则后续所有测试与产物都建立在被污染的源码上：\n  - "
        + "\n  - ".join(leftovers)
    )


def test_no_stale_git_index_lock() -> None:
    """陈旧 `.git/index.lock` 会让后续所有 git 写操作失败（实测踩到）。"""
    lock = ROOT / ".git" / "index.lock"
    assert not lock.exists(), (
        f"存在 {lock} —— 可能是被中断的 git/变异进程留下的陈旧锁。"
        "确认无活跃 git 进程后改名留档（不要直接删，先确认无人持有）。"
    )
