"""Read-only Hermes worker discovery probe; never prints credentials."""
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
    completed = await worker.start(thread["thread_id"])
    result["status"] = "PASS" if completed["status"] == "completed" else "BLOCKED"
    result["worker_status"] = completed["status"]
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
