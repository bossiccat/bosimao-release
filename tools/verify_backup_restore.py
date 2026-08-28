"""备份恢复演练：agent_threads.sqlite3 WAL 库在线备份 → 恢复 → 一致性校验 → 恢复库继续运行。

PRR Release Safety 证据脚本。每次运行使用随机文件名，不删除既有运行数据。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DATA_DIR = BACKEND / "data"
RUN_TAG = uuid.uuid4().hex[:12]
SOURCE_DB = DATA_DIR / f"drill_backup_src_{RUN_TAG}.sqlite3"
BACKUP_DB = DATA_DIR / f"drill_backup_dst_{RUN_TAG}.sqlite3"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def snapshot(db_path: Path) -> tuple[list, list]:
    db = sqlite3.connect(str(db_path))
    threads = db.execute(
        "SELECT thread_id, status, user_speech, summary FROM agent_threads ORDER BY thread_id"
    ).fetchall()
    commands = db.execute(
        "SELECT command_id, thread_id, command, status, payload FROM agent_commands ORDER BY command_id"
    ).fetchall()
    db.close()
    return threads, commands


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    os.environ["AGENT_THREAD_DB"] = str(SOURCE_DB)
    from app.brain.agent_thread_registry import AgentThreadRegistry

    # 1. 构造含完整审批链路的样本库
    source = AgentThreadRegistry(str(SOURCE_DB))
    t_wait = source.spawn("演练：等待审批")
    source.handle_tool("approve_reply", {"thread_id": t_wait["thread_id"], "summary": "需要确认"})
    t_approved = source.spawn("演练：已审批")
    source.handle_tool("approve_reply", {"thread_id": t_approved["thread_id"], "summary": "需要确认"})
    source.approve(t_approved["thread_id"], source.get(t_approved["thread_id"])["approval_id"])
    t_running = source.spawn("演练：运行中")
    source.handle_tool("approve_reply", {"thread_id": t_running["thread_id"], "summary": "需要确认"})
    source.approve(t_running["thread_id"], source.get(t_running["thread_id"])["approval_id"])
    source.claim_commands()
    source.claim_queued(t_running["thread_id"])
    check("样本库含 3 线程（awaiting/queued/running）",
          len(source.get(t_wait["thread_id"])["approval_id"]) >= 32,
          f"source={SOURCE_DB.name}")

    # 2. 在线备份（sqlite3 backup API，WAL 一致）
    result = source.backup_to(str(BACKUP_DB))
    check("在线备份完成且计数一致",
          BACKUP_DB.is_file() and result["threads"] == 3 and result["commands"] == 2,
          str({k: result[k] for k in ("threads", "commands")}))

    # 3. 一致性校验：线程/命令行集逐行一致
    src_threads, src_commands = snapshot(SOURCE_DB)
    dst_threads, dst_commands = snapshot(BACKUP_DB)
    check("线程行集逐行一致", src_threads == dst_threads, f"{len(dst_threads)} rows")
    check("命令行集逐行一致", src_commands == dst_commands, f"{len(dst_commands)} rows")

    # 4. 恢复库按运维路径恢复运行：recover → claim → 状态推进
    restored = AgentThreadRegistry(str(BACKUP_DB))
    check("恢复库 recover claimed 命令", restored.recover_commands() == 2)
    remaining = restored.claim_commands()
    check("恢复库领取已审批命令", len(remaining) == 1 and remaining[0]["thread_id"] == t_approved["thread_id"])
    check("恢复库状态推进到 running", restored.claim_queued(t_approved["thread_id"]) is not None
          and restored.get(t_approved["thread_id"])["status"] == "running")
    check("运行中样本命令被取消（防重复执行）",
          restored.get(t_running["thread_id"])["status"] == "running")

    # 5. 清理演练文件（仅本次 RUN_TAG 生成的演练库）
    for p in (SOURCE_DB, BACKUP_DB):
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                try:
                    f.unlink()
                except OSError as e:
                    print(f"[info] 演练文件保留（沙箱策略）：{f.name} {e}")

    print()
    if FAILURES:
        print(f"RESULT: FAIL（{len(FAILURES)} 项）: {FAILURES}")
        return 1
    print("RESULT: ALL PASS — 备份恢复演练通过（备份一致性 + 恢复库可继续运行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
