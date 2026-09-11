"""P0-1/F10：进程内日志持久化（RotatingFileHandler）

根因：rtc_bridge 由启动器以 -RedirectStandardOutput/-RedirectStandardError
启动，每次重启截断 rtc_bridge.log(.err) → 21:06 会话日志被 21:40 重启覆盖，归因无证据。
修复：进程内自带 RotatingFileHandler（10MB×5）直写 logs/，与启动器重定向解耦
（文件名加 _app 后缀，避免与重定向目标同文件双写互踩）。
"""
from __future__ import annotations

import logging

import pytest

from app.utils.logger import add_rotating_file_handler


def test_add_rotating_file_handler_persists_logs(tmp_path, caplog):
    """helper 应给指定 logger 挂上 RotatingFileHandler 并落盘"""
    target = logging.getLogger("test.persist.p0_1")
    target.setLevel(logging.INFO)
    target.propagate = False
    target.handlers.clear()

    handler = add_rotating_file_handler(
        "test_persist_p0_1.log", logger=target,
        log_dir=tmp_path, max_bytes=1024 * 1024, backup_count=2,
    )
    assert handler is not None, "应返回创建的 RotatingFileHandler"
    # 用 ERROR 级探针：全量回归时其他测试模块顶层 logging.disable(WARNING)
    # 是进程全局的，WARNING 及以下会被压掉
    target.error("[lat] persistence probe %s", "ok")
    handler.flush()

    log_file = tmp_path / "test_persist_p0_1.log"
    assert log_file.exists(), "日志文件应落盘"
    content = log_file.read_text(encoding="utf-8")
    assert "[lat] persistence probe ok" in content

    target.handlers.remove(handler)
    handler.close()


def test_add_rotating_file_handler_survives_log_dir_failure(tmp_path, monkeypatch):
    """logs 目录不可建时不得炸启动（返回 None，降级为仅控制台）"""

    class BadDir:
        def mkdir(self, *a, **k):
            raise OSError("disk full")

        def __truediv__(self, name):
            raise OSError("disk full")

    handler = add_rotating_file_handler(
        "bad.log", logger=logging.getLogger("test.persist.bad"),
        log_dir=BadDir(),
    )
    assert handler is None, "目录不可写应返回 None 而非抛异常"
