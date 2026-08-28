"""Persistent worker-thread registry for voice orchestration."""
from __future__ import annotations

import json
import os
import sqlite3
import secrets
import threading
import time
import uuid
from pathlib import Path

from .hermes_worker_profiles import worker_command_argv


class AgentThreadRegistry:
    def __init__(
        self,
        db_path: str | None = None,
        default_worker_profile: str = "probe_help",
    ) -> None:
        default_path = Path(__file__).resolve().parents[2] / "data" / "agent_threads.sqlite3"
        path = db_path or os.environ.get("AGENT_THREAD_DB") or default_path
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._default_worker_profile = default_worker_profile
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("CREATE TABLE IF NOT EXISTS agent_threads (thread_id TEXT PRIMARY KEY, user_speech TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_commands (
                command_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                command TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                claimed_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_agent_commands_pending ON agent_commands(status, created_at)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
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

    def request_approval(self, thread_id: str, approval_id: str = "", summary: str = "") -> dict:
        if approval_id.strip():
            return {"error": "approval_id_not_accepted"}
        summary = summary.strip()
        if not summary:
            return {"error": "invalid_approval_request"}
        generated = secrets.token_urlsafe(32)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT status, payload FROM agent_threads WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                return {"error": "thread_not_found"}
            if row["status"] == "awaiting_approval":
                return {"error": "approval_already_pending"}
            old_payload = json.loads(row["payload"] or "{}")
            payload = {"workspace": old_payload.get("workspace", ""), "approval_id": generated}
            updated = db.execute(
                "UPDATE agent_threads SET status=?, summary=?, updated_at=?, payload=? WHERE thread_id=? AND status != ?",
                ("awaiting_approval", summary[:1000], time.time(), json.dumps(payload, ensure_ascii=False), thread_id, "awaiting_approval"),
            )
            if updated.rowcount != 1:
                return {"error": "approval_already_pending"}
        return self.get(thread_id) or {"error": "thread_not_found"}

    def approve(self, thread_id: str, approval_id: str) -> dict:
        approval_id = approval_id.strip()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM agent_threads WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                return {"error": "thread_not_found"}
            if row["status"] != "awaiting_approval":
                return {"error": "approval_not_pending"}
            payload = db.execute("SELECT payload FROM agent_threads WHERE thread_id=?", (thread_id,)).fetchone()
            expected = json.loads(payload["payload"] or "{}").get("approval_id", "")
            if not secrets.compare_digest(expected, approval_id):
                return {"error": "approval_mismatch"}
            updated = db.execute(
                "UPDATE agent_threads SET status=?, summary=?, updated_at=?, payload=? WHERE thread_id=? AND status=? AND json_extract(payload, '$.approval_id')=?",
                ("queued", "审批已通过，等待后台 Worker 执行。", time.time(), json.dumps({"workspace": json.loads(payload["payload"] or "{}").get("workspace", ""), "approval_id": ""}, ensure_ascii=False), thread_id, "awaiting_approval", approval_id),
            )
            if updated.rowcount != 1:
                return {"error": "approval_not_pending"}
            db.execute(
                "INSERT INTO agent_commands(command_id, thread_id, command, payload, status, created_at, claimed_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    uuid.uuid4().hex,
                    thread_id,
                    "start_worker",
                    json.dumps(
                        {
                            "thread_id": thread_id,
                            "profile": self._default_worker_profile,
                            # 绑定摘要：审批通过时刻锁定的规范化命令（含规范二进制名，
                            # 不含凭据/用户文本/运行时绝对路径）。
                            "command_argv": [
                                "hermes",
                                *worker_command_argv(self._default_worker_profile),
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    "pending",
                    time.time(),
                ),
            )
        return self.get(thread_id) or {"error": "thread_not_found"}

    def complete_command(self, command_id: str) -> bool:
        """Mark a claimed command complete exactly once."""
        with self._lock, self._connect() as db:
            updated = db.execute(
                "UPDATE agent_commands SET status='completed' WHERE command_id=? AND status='claimed'",
                (command_id,),
            )
            return updated.rowcount == 1

    def backup_to(self, dest_path: str) -> dict:
        """Safe online backup via sqlite3 backup API (WAL-consistent, no file copy)."""
        dest = Path(dest_path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as src_db:
            src_db.execute("PRAGMA busy_timeout=5000")
            dst_db = sqlite3.connect(str(dest))
            try:
                src_db.backup(dst_db)
                dst_db.commit()
            finally:
                dst_db.close()
        with sqlite3.connect(str(dest)) as verify_db:
            threads = verify_db.execute("SELECT count(*) FROM agent_threads").fetchone()[0]
            commands = verify_db.execute("SELECT count(*) FROM agent_commands").fetchone()[0]
        return {
            "source": str(self.path),
            "dest": str(dest),
            "threads": threads,
            "commands": commands,
        }

    def last_command_payload(self, thread_id: str) -> dict | None:
        """Return the most recent start_worker command payload for a thread."""
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM agent_commands WHERE thread_id=? AND command='start_worker' ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"] or "{}") or None
        except json.JSONDecodeError:
            return None

    def recover_commands(self) -> int:
        """Return commands left claimed by a bridge that was restarted."""
        with self._lock, self._connect() as db:
            updated = db.execute(
                "UPDATE agent_commands SET status='pending', claimed_at=NULL WHERE status='claimed'"
            )
            return updated.rowcount

    def claim_commands(self, limit: int = 20) -> list[dict]:
        """Atomically claim approved worker commands across bridge processes."""
        limit = max(1, min(int(limit), 100))
        claimed: list[dict] = []
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT command_id, thread_id, command, payload FROM agent_commands WHERE status='pending' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            now = time.time()
            for row in rows:
                thread = db.execute("SELECT status FROM agent_threads WHERE thread_id=?", (row["thread_id"],)).fetchone()
                if thread is None or thread["status"] != "queued":
                    db.execute("UPDATE agent_commands SET status='cancelled', claimed_at=? WHERE command_id=? AND status='pending'", (now, row["command_id"]))
                    continue
                updated = db.execute("UPDATE agent_commands SET status='claimed', claimed_at=? WHERE command_id=? AND status='pending'", (now, row["command_id"]))
                if updated.rowcount == 1:
                    item = dict(row)
                    item["payload"] = json.loads(item["payload"] or "{}")
                    claimed.append(item)
        return claimed

    def claim_queued(self, thread_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            updated = db.execute(
                "UPDATE agent_threads SET status=?, summary=?, updated_at=? WHERE thread_id=? AND status=?",
                ("running", "后台 Worker 已原子领取任务。", time.time(), thread_id, "queued"),
            )
            if updated.rowcount != 1:
                return None
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
            row = self.spawn(str(args.get("user_speech", "")), str(args.get("workspace", "")))
            return self.request_approval(row["thread_id"], "", "需要用户确认后启动只读后台 Worker")
        if name == "agent_status":
            return self.get(str(args.get("thread_id", ""))) or {"error": "thread_not_found"}
        if name == "steer_agent_thread":
            return self.steer(str(args.get("thread_id", "")), str(args.get("instruction", "")), str(args.get("action", "steer"))) or {"error": "thread_not_found"}
        if name == "approve_reply":
            return self.request_approval(
                str(args.get("thread_id", "")),
                str(args.get("approval_id", "")),
                str(args.get("summary", "")),
            )
        return {"error": "unknown_tool"}
