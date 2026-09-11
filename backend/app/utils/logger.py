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


def add_rotating_file_handler(
    log_file_name: str,
    logger: logging.Logger | None = None,
    log_dir=None,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Handler | None:
    """P0-1/F10：进程内日志持久化——RotatingFileHandler 直写 logs/<name>

    背景：PC 端启动器用 -RedirectStandardOutput/Error 启动 rtc_bridge/relay，
    每次重启截断重定向文件（2026-09-06 实锤：21:06 会话日志被 21:40 重启覆盖）。
    进程内自带滚动文件与重定向解耦（文件名用 *_app.log，避免与重定向目标同路径双写）。
    目录不可写等异常一律降级为 None（只留控制台），绝不阻断进程启动。
    """
    target = logger if logger is not None else logging.getLogger()
    try:
        base = log_dir if log_dir is not None else _LOG_DIR
        base.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            base / log_file_name,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as e:
        logging.getLogger(__name__).warning(
            "rotating file log unavailable (%s): %s", log_file_name, e
        )
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                                          datefmt="%Y-%m-%d %H:%M:%S"))
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    target.addHandler(handler)
    return handler
