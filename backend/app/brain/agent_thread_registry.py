"""Persistent worker-thread registry for voice orchestration."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class AgentThreadRegistry:
    def __init__(self, db_path: str | None = None) -> None:
        path = db_path or os.environ.get("AGENT_THREAD_DB", "backend/data/agent_threads.sqlite3")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS agent_threads (thread_id TEXT PRIMARY KEY, user_speech TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL)")

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def spawn(self, user_speech: str, workspace: str = "") -> dict:
        now = time.time()
        thread_id = "ath-" + uuid.uuid4().hex[:16]
        row = {"thread_id": thread_id, "user_speech": user_speech[:4000], "status": "queued", "summary": "任务已接收，等待后台 Worker 执行。", "created_at": now, "updated_at": now, "workspace": workspace}
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO agent_threads VALUES (?, ?, ?, ?, ?, ?, ?)", (thread_id, row["user_speech"], row["status"], row["summary"], now, now, json.dumps({"workspace": workspace}, ensure_ascii=False)))
        return row

    def get(self, thread_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM agent_threads WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("payload") or "{}"))
        return result

    def update(self, thread_id: str, status: str, summary: str) -> dict | None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE agent_threads SET status=?, summary=?, updated_at=? WHERE thread_id=?", (status, summary[:1000], time.time(), thread_id))
        return self.get(thread_id)

    def steer(self, thread_id: str, instruction: str = "", action: str = "steer") -> dict | None:
        row = self.get(thread_id)
        if row is None:
            return None
        status = "cancelled" if action == "cancel" else "steered"
        summary = "后台任务已取消。" if action == "cancel" else "后台任务已收到追加指令。"
        with self._lock, self._connect() as db:
            db.execute("UPDATE agent_threads SET status=?, summary=?, updated_at=? WHERE thread_id=?", (status, summary, time.time(), thread_id))
        return self.get(thread_id)

    def handle_tool(self, name: str, args: dict) -> dict:
        if name == "spawn_agent_thread":
            return self.spawn(str(args.get("user_speech", "")), str(args.get("workspace", "")))
        if name == "agent_status":
            return self.get(str(args.get("thread_id", ""))) or {"error": "thread_not_found"}
        if name == "steer_agent_thread":
            return self.steer(str(args.get("thread_id", "")), str(args.get("instruction", "")), str(args.get("action", "steer"))) or {"error": "thread_not_found"}
        if name == "approve_reply":
            return {"thread_id": str(args.get("thread_id", "")), "approval_id": str(args.get("approval_id", "")), "status": "approval_required", "summary": str(args.get("summary", ""))}
        return {"error": "unknown_tool"}
