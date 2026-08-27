from __future__ import annotations

from pathlib import Path

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


def test_default_database_path_is_not_derived_from_process_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("AGENT_THREAD_DB", raising=False)
    monkeypatch.chdir(tmp_path)

    registry = AgentThreadRegistry()

    expected = Path(__file__).resolve().parents[2] / "data" / "agent_threads.sqlite3"
    assert registry.path == expected


def test_atomic_claim_consumes_queued_thread_once(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("只读探测")
    first = registry.claim_queued(thread["thread_id"])
    second = registry.claim_queued(thread["thread_id"])
    assert first is not None
    assert first["status"] == "running"
    assert second is None


def test_unknown_tool_and_thread_are_safe(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    assert registry.handle_tool("agent_status", {"thread_id": "missing"})["error"] == "thread_not_found"
    assert registry.handle_tool("not_a_tool", {})["error"] == "unknown_tool"


def test_spawn_requires_server_generated_approval_and_waits_without_running_worker(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.handle_tool("spawn_agent_thread", {"user_speech": "修改项目配置"})

    assert thread["status"] == "awaiting_approval"
    assert len(thread["approval_id"]) >= 32
    assert thread["approval_id"] != "approval-1"


def test_model_cannot_supply_approval_id(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("修改项目配置")
    result = registry.handle_tool(
        "approve_reply",
        {"thread_id": thread["thread_id"], "approval_id": "forged", "summary": "确认"},
    )
    assert result["error"] == "approval_id_not_accepted"


def test_approval_release_requires_matching_id_and_only_releases_waiting_thread(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("执行受控任务")
    registry.handle_tool(
        "approve_reply",
        {
            "thread_id": thread["thread_id"],
            "summary": "需要确认",
        },
    )

    pending = registry.get(thread["thread_id"])
    assert registry.approve(thread["thread_id"], "wrong")["error"] == "approval_mismatch"
    released = registry.approve(thread["thread_id"], pending["approval_id"])
    assert released["status"] == "queued"
    assert released["approval_id"] == ""


def test_cancelled_thread_cannot_be_resurrected_by_a_late_approval(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("执行受控任务")
    registry.handle_tool(
        "approve_reply",
        {
            "thread_id": thread["thread_id"],
            "approval_id": "approval-4",
            "summary": "需要确认",
        },
    )
    registry.steer(thread["thread_id"], action="cancel")

    assert registry.approve(thread["thread_id"], "approval-4")["error"] == "approval_not_pending"
    assert registry.get(thread["thread_id"])["status"] == "cancelled"


def test_duplicate_approval_request_does_not_replace_pending_approval(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("执行受控任务")
    registry.handle_tool(
        "approve_reply",
        {
            "thread_id": thread["thread_id"],
            "summary": "第一次确认",
        },
    )

    result = registry.handle_tool(
        "approve_reply",
        {
            "thread_id": thread["thread_id"],
            "approval_id": "approval-replacement",
            "summary": "替换确认",
        },
    )

    assert result["error"] == "approval_id_not_accepted"
    assert registry.get(thread["thread_id"])["approval_id"]


def test_approval_enqueues_command_visible_to_independent_registry(tmp_path):
    db = tmp_path / "threads.sqlite3"
    api_registry = AgentThreadRegistry(str(db))
    bridge_registry = AgentThreadRegistry(str(db))
    thread = api_registry.spawn("执行受控任务")
    pending = api_registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })

    released = api_registry.approve(thread["thread_id"], pending["approval_id"])
    commands = bridge_registry.claim_commands()

    assert released["status"] == "queued"
    assert len(commands) == 1
    assert commands[0]["thread_id"] == thread["thread_id"]
    assert bridge_registry.claim_commands() == []


def test_claimed_command_is_recovered_after_bridge_restart(tmp_path):
    db = tmp_path / "threads.sqlite3"
    api_registry = AgentThreadRegistry(str(db))
    bridge_registry = AgentThreadRegistry(str(db))
    thread = api_registry.spawn("重启恢复任务")
    pending = api_registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    api_registry.approve(thread["thread_id"], pending["approval_id"])
    command = bridge_registry.claim_commands()[0]
    assert command["thread_id"] == thread["thread_id"]
    assert bridge_registry.recover_commands() == 1
    assert bridge_registry.claim_commands()[0]["thread_id"] == thread["thread_id"]


def test_completed_command_is_not_replayed_after_restart(tmp_path):
    db = tmp_path / "threads.sqlite3"
    registry = AgentThreadRegistry(str(db))
    thread = registry.spawn("一次启动任务")
    pending = registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    registry.approve(thread["thread_id"], pending["approval_id"])
    command = registry.claim_commands()[0]
    assert registry.claim_queued(command["thread_id"]) is not None
    assert registry.complete_command(command["command_id"]) is True
    assert registry.recover_commands() == 0
    assert registry.claim_commands() == []


def test_cancelled_late_approval_command_is_not_claimed(tmp_path):
    db = tmp_path / "threads.sqlite3"
    api_registry = AgentThreadRegistry(str(db))
    bridge_registry = AgentThreadRegistry(str(db))
    thread = api_registry.spawn("执行受控任务")
    pending = api_registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    api_registry.approve(thread["thread_id"], pending["approval_id"])
    api_registry.steer(thread["thread_id"], action="cancel")

    assert bridge_registry.claim_commands() == []
    assert bridge_registry.get(thread["thread_id"])["status"] == "cancelled"
