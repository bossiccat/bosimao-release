"""任务仓库：内存 + JSON 持久化（backend-brain-spec §2 backend/data/brain_tasks.json）

- 不入 git（运行时生成）
- 损坏文件容忍：加载失败仅 warning，从空开始
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..config import PROJECT_ROOT
from .schemas import BrainTask

logger = logging.getLogger(__name__)

DEFAULT_PATH = PROJECT_ROOT / "backend" / "data" / "brain_tasks.json"


class TaskStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_PATH
        self._tasks: dict[str, BrainTask] = {}
        self._lock = threading.Lock()
        self._load()

    # ---------- 读 ----------
    def get(self, task_id: str) -> BrainTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(
        self, status: str | None = None, page: int = 1, limit: int = 20
    ) -> tuple[list[BrainTask], int]:
        with self._lock:
            items = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        if status:
            items = [t for t in items if t.status == status]
        total = len(items)
        start = (page - 1) * limit
        return items[start : start + limit], total

    # ---------- 写 ----------
    def create(self, task: BrainTask) -> BrainTask:
        with self._lock:
            self._tasks[task.task_id] = task
        self._save()
        return task

    def update(self, task: BrainTask) -> BrainTask:
        with self._lock:
            self._tasks[task.task_id] = task
        self._save()
        return task

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw:
                task = BrainTask.model_validate(item)
                self._tasks[task.task_id] = task
            logger.info("brain store loaded: %d tasks from %s", len(self._tasks), self._path)
        except Exception as e:  # noqa: BLE001 - 损坏容忍
            logger.warning("brain store 加载失败（从空开始）: %s", e)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = [t.model_dump(mode="json") for t in self._tasks.values()]
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("brain store 持久化失败（仅内存）: %s", e)
