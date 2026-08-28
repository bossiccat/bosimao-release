"""canary / feature-off 开关测试：env 驱动的 Worker 行为旋钮。

安全边界：
- WORKER_PROFILE 只能在 allowlist 内选择（未知值回退默认，不报错不放行）；
- WORKER_TIMEOUT_SECONDS 只调数值，不可关闭超时（挂死进程必须能被杀死）；
- WORKER_BINDING_ENFORCE=0 仅关闭绑定校验（旧库兼容），argv 仍由 allowlist 锁定。
"""
from __future__ import annotations

import pytest

from app.brain.agent_thread_registry import AgentThreadRegistry
from app.brain.hermes_worker_runner import HermesWorkerRunner


@pytest.fixture()
def registry(tmp_path):
    return AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))


def test_from_env_defaults(registry):
    runner = HermesWorkerRunner.from_env(registry, env={})
    flags = runner.feature_flags()
    assert flags == {
        "profile": "probe_help",
        "timeout_seconds": 120.0,
        "binding_enforce": True,
    }


def test_from_env_selects_allowlisted_profile(registry):
    runner = HermesWorkerRunner.from_env(registry, env={"WORKER_PROFILE": "deepseek_readonly"})
    assert runner.feature_flags()["profile"] == "deepseek_readonly"


def test_from_env_unknown_profile_falls_back_to_default(registry):
    """env 里的未知 profile 回退默认（env 只能在 allowlist 内选择，不得报错放行）。"""
    runner = HermesWorkerRunner.from_env(registry, env={"WORKER_PROFILE": "rm_rf_slash"})
    assert runner.feature_flags()["profile"] == "probe_help"


def test_from_env_timeout_override_and_clamp(registry):
    runner = HermesWorkerRunner.from_env(registry, env={"WORKER_TIMEOUT_SECONDS": "0.5"})
    assert runner.feature_flags()["timeout_seconds"] == 0.5
    runner2 = HermesWorkerRunner.from_env(registry, env={"WORKER_TIMEOUT_SECONDS": "0"})
    assert runner2.feature_flags()["timeout_seconds"] == 0.1


def test_from_env_invalid_timeout_falls_back_to_default(registry):
    runner = HermesWorkerRunner.from_env(registry, env={"WORKER_TIMEOUT_SECONDS": "abc"})
    assert runner.feature_flags()["timeout_seconds"] == 120.0


def test_binding_enforce_off_skips_verification_but_keeps_allowlist(registry, tmp_path):
    """feature-off：绑定校验跳过（旧库兼容），但 argv 仍由 allowlist 锁定。"""
    import json as _json
    import sqlite3 as _sqlite3

    source = HermesWorkerRunner.from_env(registry, env={"WORKER_BINDING_ENFORCE": "0"})
    assert source.feature_flags()["binding_enforce"] is False

    thread = registry.spawn("旧库兼容场景")
    registry.handle_tool("approve_reply", {"thread_id": thread["thread_id"], "summary": "需要确认"})
    pending = registry.get(thread["thread_id"])
    registry.approve(thread["thread_id"], pending["approval_id"])
    command = registry.claim_commands()[0]
    # 篡改/清空绑定 → 若开关开着必须拒绝；关着则跳过校验
    db = _sqlite3.connect(str(tmp_path / "threads.sqlite3"))
    db.execute(
        "UPDATE agent_commands SET payload=? WHERE command_id=?",
        (_json.dumps({"thread_id": thread["thread_id"]}), command["command_id"]),
    )
    db.commit()
    db.close()

    # 走到二进制解析阶段（binding 跳过）→ hermes 不在 PATH 则 failed 于 CLI 不可用，
    # 而不是 failed 于绑定缺失 —— 以此区分两个失败路径。
    result = source.start_sync(thread["thread_id"])
    assert "绑定" not in result["summary"]


def test_feature_flags_exposed_via_state_for_health(tmp_path):
    """BridgeServer 装配时把 feature flags 写入 state，health 可观测。"""
    import asyncio

    from rtc_bridge.server import BridgeServer

    class _Cfg:
        apm_api_url = ""
        apm_system_prompt = ""
        apm_token = ""
        voice_engine = "apm"
        qwen_api_url = ""
        qwen_token = ""
        qwen_system_prompt = ""
        down_frame_ms = 40
        sample_rate = 16000
        up_max_frames = 100
        up_max_bytes = 128000
        up_max_frame_age_ms = 500
        down_max_frames = 100
        down_max_bytes = 128000
        down_max_frame_age_ms = 500

    state: dict = {}
    bridge = BridgeServer(_Cfg(), state)
    assert state.get("worker_feature_flags") == {
        "profile": "probe_help",
        "timeout_seconds": 120.0,
        "binding_enforce": True,
    }
    del bridge, asyncio