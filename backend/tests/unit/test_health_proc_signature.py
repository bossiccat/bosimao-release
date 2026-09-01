"""A10：/health 进程签名契约测试（2026-08-21 numpy 事故修复）

事故机制：临时 python 进程占 :8000 且 /health 返回 200 → 启动脚本幂等放行，
服务实际不可用但被判定健康。修复：/health 携带 proc_name/pid/run_id，
消费方可核对"应答进程 = 期望进程"。

契约（backend /health 与 rtc_bridge /health 一致）：
- status == "ok"（原有字段，兼容 watchdog/scripts 只查 status 的消费方）
- proc_name 非空字符串（sys.executable 的 basename）
- pid == 实际进程 PID（int）
- run_id 为 8 位 hex（uuid4 前 8 位，进程生命周期内稳定）
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest


def _run(coro):
    return asyncio.run(coro)


# ---------------- backend app.main /health ----------------

def test_backend_health_has_proc_signature():
    from app.main import _PROC_NAME, _PROC_PID, _RUN_ID, health

    resp = _run(health())
    assert resp["status"] == "ok"
    assert isinstance(_PROC_NAME, str) and _PROC_NAME
    assert resp["proc_name"] == _PROC_NAME
    assert isinstance(_PROC_PID, int)
    assert resp["pid"] == _PROC_PID == os.getpid()
    assert re.fullmatch(r"[0-9a-f]{8}", _RUN_ID) is not None
    assert resp["run_id"] == _RUN_ID


def test_backend_health_signature_module_level_stable():
    """签名是模块级常量：同一进程内多次调用 /health 结果一致（消费方可做变化检测）"""
    from app.main import _RUN_ID, health

    r1 = _run(health())
    r2 = _run(health())
    assert r1["run_id"] == r2["run_id"] == _RUN_ID
    assert r1["pid"] == r2["pid"]


# ---------------- rtc_bridge /health ----------------

@pytest.mark.asyncio
async def test_rtc_bridge_health_has_proc_signature():
    """rtc_bridge HealthServer /health 响应同样带进程签名（pythonw 承载属正常，仅作标识）"""
    from rtc_bridge import health as health_mod

    hs = health_mod.HealthServer("127.0.0.1", 0, {})
    reader = asyncio.StreamReader()
    reader.feed_data(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
    reader.feed_eof()

    class Writer:
        def __init__(self) -> None:
            self.data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    writer = Writer()
    await hs._handle(reader, writer)
    assert b"200 OK" in writer.data
    body = writer.data.split(b"\r\n\r\n", 1)[1]
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == "ok"
    assert isinstance(payload["proc_name"], str) and payload["proc_name"]
    assert payload["proc_name"] == health_mod._PROC_NAME
    assert payload["pid"] == health_mod._PROC_PID == os.getpid()
    assert re.fullmatch(r"[0-9a-f]{8}", payload["run_id"]) is not None
    assert payload["run_id"] == health_mod._RUN_ID
