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


def test_approved_command_carries_pinned_worker_profile_binding(tmp_path):
    """approve() 写入的 start_worker 命令必须携带 profile 绑定摘要（不含凭据）。"""
    db = tmp_path / "threads.sqlite3"
    registry = AgentThreadRegistry(str(db))
    thread = registry.spawn("执行受控任务")
    pending = registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    registry.approve(thread["thread_id"], pending["approval_id"])

    commands = registry.claim_commands()

    assert len(commands) == 1
    payload = commands[0]["payload"]
    assert payload.get("profile") == "probe_help"
    assert tuple(payload.get("command_argv", [])) == ("hermes", "--help")
    assert all("HERMES_CUSTOM_KKDMX_API_KEY" not in str(v) for v in payload.values())


def test_approved_deepseek_profile_binding_pins_readonly_route(tmp_path):
    """受限 profile 的绑定摘要必须锁定显式 DeepSeek 只读路由。"""
    db = tmp_path / "threads.sqlite3"
    registry = AgentThreadRegistry(str(db), default_worker_profile="deepseek_readonly")
    thread = registry.spawn("执行受控任务")
    pending = registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    registry.approve(thread["thread_id"], pending["approval_id"])

    payload = registry.claim_commands()[0]["payload"]

    assert payload.get("profile") == "deepseek_readonly"
    assert tuple(payload.get("command_argv", [])) == (
        "hermes", "-z",
        "--model", "deepseek-v4-flash-0731",
        "--provider", "kkdmx",
        "--toolsets", "",
        "ping",
    )


def test_command_payload_without_profile_binding_is_rejected_at_launch(tmp_path):
    """旧格式命令（无 profile 绑定）必须在启动阶段被拒绝，不允许静默降级。"""
    from app.brain.hermes_worker_runner import HermesWorkerRunner
    import json as _json
    import sqlite3 as _sqlite3
    import time as _time
    import uuid as _uuid

    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("旧格式任务")
    registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    pending = registry.get(thread["thread_id"])
    registry.approve(thread["thread_id"], pending["approval_id"])
    command = registry.claim_commands()[0]
    # 手工把命令 payload 降级为无绑定的旧格式（模拟篡改/旧版本写入）
    db = _sqlite3.connect(str(tmp_path / "threads.sqlite3"))
    db.execute(
        "UPDATE agent_commands SET payload=? WHERE command_id=?",
        (_json.dumps({"thread_id": thread["thread_id"]}), command["command_id"]),
    )
    db.commit()
    db.close()

    runner = HermesWorkerRunner(registry, hermes_bin="hermes", profile="probe_help")
    result = runner.start_sync(thread["thread_id"])

    assert result["status"] == "failed"
    assert "绑定" in result["summary"] or "binding" in result["summary"].lower()


def test_worker_runner_refuses_command_with_tampered_profile_binding(tmp_path):
    """命令 payload 的 profile 绑定与 runner 配置不一致时，启动必须失败。"""
    from app.brain.hermes_worker_runner import HermesWorkerRunner
    import json as _json
    import sqlite3 as _sqlite3

    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    thread = registry.spawn("篡改场景")
    registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "需要确认",
    })
    pending = registry.get(thread["thread_id"])
    registry.approve(thread["thread_id"], pending["approval_id"])
    command = registry.claim_commands()[0]
    db = _sqlite3.connect(str(tmp_path / "threads.sqlite3"))
    db.execute(
        "UPDATE agent_commands SET payload=? WHERE command_id=?",
        (_json.dumps({"thread_id": thread["thread_id"], "profile": "deepseek_readonly", "command_argv": ["hermes", "-z", "evil"]}), command["command_id"]),
    )
    db.commit()
    db.close()

    runner = HermesWorkerRunner(registry, hermes_bin="hermes", profile="probe_help")
    result = runner.start_sync(thread["thread_id"])

    assert result["status"] == "failed"
    assert "绑定" in result["summary"] or "binding" in result["summary"].lower()


def test_backup_restores_threads_commands_and_stays_operational(tmp_path):
    """WAL 库备份→恢复后：线程/命令行集逐行一致，且恢复库可继续运行审批链路。"""
    import sqlite3 as _sqlite3

    source = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    # 造含完整审批链路的样本：awaiting + approved 命令 + 已领取运行中
    t_wait = source.spawn("等待审批样本")
    source.handle_tool("approve_reply", {"thread_id": t_wait["thread_id"], "summary": "需要确认"})
    t_approved = source.spawn("已审批样本")
    source.handle_tool("approve_reply", {"thread_id": t_approved["thread_id"], "summary": "需要确认"})
    source.approve(t_approved["thread_id"], source.get(t_approved["thread_id"])["approval_id"])
    t_running = source.spawn("运行中样本")
    source.handle_tool("approve_reply", {"thread_id": t_running["thread_id"], "summary": "需要确认"})
    source.approve(t_running["thread_id"], source.get(t_running["thread_id"])["approval_id"])
    source.claim_commands()
    source.claim_queued(t_running["thread_id"])

    backup_path = tmp_path / "backup" / "threads_backup.sqlite3"
    result = source.backup_to(str(backup_path))

    assert backup_path.is_file()
    assert result["threads"] >= 3
    assert result["commands"] >= 1

    def snapshot(db_path):
        db = _sqlite3.connect(str(db_path))
        threads = db.execute(
            "SELECT thread_id, status, user_speech, summary FROM agent_threads ORDER BY thread_id"
        ).fetchall()
        commands = db.execute(
            "SELECT command_id, thread_id, command, status, payload FROM agent_commands ORDER BY command_id"
        ).fetchall()
        db.close()
        return threads, commands

    src_threads, src_commands = snapshot(tmp_path / "threads.sqlite3")
    dst_threads, dst_commands = snapshot(backup_path)

    assert src_threads == dst_threads
    assert src_commands == dst_commands

    # 恢复库必须可继续运行：按运维恢复路径先 recover（claimed→pending），再领取推进。
    # 运行中样本的命令因线程已 running 会被自动取消（防重复执行），属预期行为。
    restored = AgentThreadRegistry(str(backup_path))
    assert restored.recover_commands() == 2
    remaining = restored.claim_commands()
    assert len(remaining) == 1
    assert remaining[0]["thread_id"] == t_approved["thread_id"]
    assert restored.claim_queued(t_approved["thread_id"]) is not None
    assert restored.get(t_approved["thread_id"])["status"] == "running"
    assert restored.get(t_running["thread_id"])["status"] == "running"
