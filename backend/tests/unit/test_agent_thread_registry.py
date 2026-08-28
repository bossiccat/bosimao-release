from __future__ import annotations

from app.brain.agent_thread_registry import AgentThreadRegistry


def test_spawn_status_and_steer_survive_new_registry_instance(tmp_path):
    db = tmp_path / "threads.sqlite3"
    first = AgentThreadRegistry(str(db))
    created = first.spawn("巡检客户端前后端不对称 Bug", "监视app")
    assert created["status"] == "queued"

    restored = AgentThreadRegistry(str(db))
    status = restored.get(created["thread_id"])
    assert status["user_speech"] == "巡检客户端前后端不对称 Bug"
    steered = restored.steer(created["thread_id"], "先查语音链路", "steer")
    assert steered["status"] == "steered"


def test_unknown_tool_and_thread_are_safe(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    assert registry.handle_tool("agent_status", {"thread_id": "missing"})["error"] == "thread_not_found"
    assert registry.handle_tool("not_a_tool", {})["error"] == "unknown_tool"
