"""结构化日志（JSON lines + SLI 打点 + 滚动文件）"""
from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from .._frozen_paths import project_root

_LOG_DIR = project_root() / "logs"
# ADR-010 文件堆积对策：单文件 5MB，保留 7 个备份（jax.log / jax.log.1..7）
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 7


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_file: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter())
    root.addHandler(console)

    if log_file:
        _LOG_DIR.mkdir(exist_ok=True)
        fh = RotatingFileHandler(
            _LOG_DIR / "jax.log",
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
