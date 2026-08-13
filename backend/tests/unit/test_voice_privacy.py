"""本地隐私四类开关服务验收测试（SPEC §9.2 / AC-17 / ADR-018）

- setter 返回 {applied_at, effective_value, action_result}
- 四类动作开关（云端处理/麦克风/后台对话/桌面捕获）触发对应 runtime action 并持久化
- 运行时动作失败 → SQLite 设置值回滚（保持原值）+ 动作回滚路径被调用
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.voice.privacy import (
    FakeRuntimeActions,
    PrivacyService,
    RuntimeActions,
    ToggleSettings,
)
from app.voice.storage import VoiceStore

TOGGLE_SETTINGS = [
    "cloud_processing_enabled",
    "microphone_enabled",
    "background_conversation_enabled",
    "desktop_capture_enabled",
]


def _service(tmp_path: Path, actions: RuntimeActions | None = None):
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    actions = actions or FakeRuntimeActions()
    return PrivacyService(store, actions=actions), store, actions


def test_setter_returns_contract_fields(tmp_path: Path) -> None:
    svc, _store, _actions = _service(tmp_path)
    result = svc.set("microphone_enabled", False)
    assert set(result) == {"applied_at", "effective_value", "action_result"}
    assert result["effective_value"] is False
    assert result["action_result"] == "ok"
    assert result["applied_at"] > 0
    assert result["applied_at"] <= time.time() + 1


@pytest.mark.parametrize("setting", TOGGLE_SETTINGS)
def test_each_toggle_triggers_action_and_persists(tmp_path: Path, setting: str) -> None:
    svc, store, actions = _service(tmp_path)
    assert svc.get(setting) is True  # 默认开启
    result = svc.set(setting, False)
    assert result["action_result"] == "ok"
    assert actions.applied[-1] == (setting, False)
    # SQLite 持久化：新 service 实例读到 False
    svc2 = PrivacyService(store, actions=FakeRuntimeActions())
    assert svc2.get(setting) is False
    # 重新开启
    svc2.set(setting, True)
    svc3 = PrivacyService(store, actions=FakeRuntimeActions())
    assert svc3.get(setting) is True


def test_action_failure_rolls_back_setting_value(tmp_path: Path) -> None:
    actions = FakeRuntimeActions()
    actions.fail_on.add("microphone_enabled")
    svc, store, _actions = _service(tmp_path, actions)
    assert svc.get("microphone_enabled") is True
    result = svc.set("microphone_enabled", False)
    assert result["action_result"] == "failed"
    assert result["effective_value"] is True  # 回滚为原值
    # SQLite 设置值保持原值（回滚）
    svc2 = PrivacyService(store, actions=FakeRuntimeActions())
    assert svc2.get("microphone_enabled") is True
    # 动作层回滚路径被调用（编排了回滚）
    assert actions.rolled_back == [("microphone_enabled", True)]


def test_action_failure_does_not_corrupt_other_settings(tmp_path: Path) -> None:
    actions = FakeRuntimeActions()
    actions.fail_on.add("cloud_processing_enabled")
    svc, store, _actions = _service(tmp_path, actions)
    svc.set("microphone_enabled", False)  # 成功
    result = svc.set("cloud_processing_enabled", False)  # 失败
    assert result["action_result"] == "failed"
    assert svc.get("microphone_enabled") is False
    assert svc.get("cloud_processing_enabled") is True


def test_unknown_setting_rejected(tmp_path: Path) -> None:
    svc, _store, _actions = _service(tmp_path)
    with pytest.raises(ValueError):
        svc.set("not_a_setting", False)


def test_all_four_settings_default_true(tmp_path: Path) -> None:
    svc, _store, _actions = _service(tmp_path)
    for setting in TOGGLE_SETTINGS:
        assert svc.get(setting) is True


def test_toggle_settings_contract_matches_spec() -> None:
    assert set(ToggleSettings) == {
        "cloud_processing_enabled",
        "microphone_enabled",
        "background_conversation_enabled",
        "desktop_capture_enabled",
    }
