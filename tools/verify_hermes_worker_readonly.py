"""Read-only Hermes worker discovery probe; never prints credentials.

2026-08-28 升级：绑定校验（任务 #159）生效后，直接 start() 会被
"审批命令缺少 profile 绑定" 正确拒绝 —— 探测必须走完整审批流：
spawn → request_approval → approve（持久化 profile+argv 绑定）→ start。
绑定拒绝本身即安全控制生效的正面证据。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.brain.agent_thread_registry import AgentThreadRegistry
from app.brain.hermes_worker_runner import HermesWorkerRunner


async def main() -> int:
    binary = shutil.which("hermes")
    result = {
        "binary_available": binary is not None,
        "credential_present": bool(os.environ.get("HERMES_CUSTOM_KKDMX_API_KEY")),
        "status": "BLOCKED",
    }
    if binary is None:
        result["reason"] = "Hermes CLI 不在当前进程 PATH 中"
        print(json.dumps(result, ensure_ascii=False))
        return 1

    db_path = ROOT / "backend" / "data" / "verify_hermes_worker.sqlite3"
    registry = AgentThreadRegistry(str(db_path))
    worker = HermesWorkerRunner(registry, hermes_bin=binary)

    thread = registry.spawn("只读 Hermes CLI 探测")
    thread_id = thread["thread_id"]

    requested = registry.request_approval(thread_id, summary="只读 Hermes CLI 探测（probe_help，无副作用）")
    approval_id = str(requested.get("approval_id", ""))
    if not approval_id:
        result["reason"] = f"request_approval 未返回 approval_id: {requested}"
        result["worker_status"] = requested.get("status", "")
        print(json.dumps(result, ensure_ascii=False))
        return 1

    approved = registry.approve(thread_id, approval_id)
    if approved.get("status") != "queued":
        result["reason"] = f"approve 未产生 queued 状态: {approved}"
        result["worker_status"] = approved.get("status", "")
        print(json.dumps(result, ensure_ascii=False))
        return 1

    completed = await worker.start(thread_id)
    result["status"] = "PASS" if completed["status"] == "completed" else "BLOCKED"
    result["worker_status"] = completed["status"]
    result["thread_id"] = thread_id
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
