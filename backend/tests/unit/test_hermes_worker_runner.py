"""Hermes worker lifecycle safety tests."""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.brain.agent_thread_registry import AgentThreadRegistry
from app.brain.hermes_worker_runner import HermesWorkerRunner


def test_runner_refuses_when_hermes_binary_missing(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(registry, hermes_bin="missing-hermes")
    thread = registry.spawn("检查项目状态")

    result = asyncio.run(runner.start(thread["thread_id"]))

    assert result["status"] == "failed"
    assert "Hermes CLI" in result["summary"]


@pytest.mark.asyncio
async def test_runner_records_running_then_completed(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(
        registry,
        hermes_bin=sys.executable,
        command_factory=lambda _: [sys.executable, "-c", "print('ok')"],
    )
    thread = registry.spawn("只读检查")

    result = await runner.start(thread["thread_id"])
    restored = AgentThreadRegistry(str(tmp_path / "threads.sqlite3")).get(thread["thread_id"])

    assert result["status"] == "completed"
    assert restored is not None
    assert restored["status"] == "completed"
    assert "完成" in restored["summary"]


@pytest.mark.asyncio
async def test_cancel_terminates_live_worker_and_persists_cancelled(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(
        registry,
        hermes_bin=sys.executable,
        command_factory=lambda _: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    thread = registry.spawn("慢任务")

    running = asyncio.create_task(runner.start(thread["thread_id"]))
    await runner.wait_until_running(thread["thread_id"])
    assert await runner.cancel(thread["thread_id"])
    await running

    restored = registry.get(thread["thread_id"])
    assert restored is not None
    assert restored["status"] == "cancelled"


def test_default_probe_is_fixed_and_contains_no_credential_name(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(registry, hermes_bin="hermes")

    command = tuple(runner._command_factory("hermes"))

    assert command == ("hermes", "--help")
    assert all("HERMES_CUSTOM_KKDMX_API_KEY" not in part for part in command)
