"""本地隐私服务（SPEC §9.2 / AC-17 / ADR-018）

四类动作开关 + 转写持久化开关由同一 service 编排 SQLite 写入与运行时动作：
先写设置 → 执行 runtime action → 失败回滚设置值（保持原值）并调用动作回滚路径。
setter 返回 {applied_at, effective_value, action_result}。
"""
from __future__ import annotations

import json
import time
from typing import Protocol

from .storage import VoiceStore

# 四类动作开关（AC-17）；转写持久化开关单独管理
ToggleSettings = {
    "cloud_processing_enabled",
    "microphone_enabled",
    "background_conversation_enabled",
    "desktop_capture_enabled",
}
PERSISTENCE_SETTING = "transcript_persistence_enabled"

_SETTING_PREFIX = "privacy:"


class RuntimeActions(Protocol):
    """运行时动作适配器：apply 失败抛异常 → service 回滚设置"""

    def apply(self, setting: str, enabled: bool) -> None: ...

    def rollback(self, setting: str, previous: bool) -> None: ...


class FakeRuntimeActions(RuntimeActions):
    """内存 fake 适配器（测试/开发用）：记录调用；可注入指定 setting 失败"""

    def __init__(self) -> None:
        self.applied: list[tuple[str, bool]] = []
        self.rolled_back: list[tuple[str, bool]] = []
        self.fail_on: set[str] = set()

    def apply(self, setting: str, enabled: bool) -> None:
        if setting in self.fail_on:
            raise RuntimeError(f"runtime action failed: {setting}")
        self.applied.append((setting, enabled))

    def rollback(self, setting: str, previous: bool) -> None:
        self.rolled_back.append((setting, previous))


class PrivacyService:
    def __init__(self, store: VoiceStore, actions: RuntimeActions) -> None:
        self._store = store
        self._actions = actions

    def get(self, setting: str) -> bool:
        """读取设置；四类动作开关默认 True（能力默认开启），转写持久化默认 False（AC-16）"""
        if setting not in ToggleSettings and setting != PERSISTENCE_SETTING:
            raise ValueError(f"未知隐私设置: {setting}")
        raw = self._store.get_setting(_SETTING_PREFIX + setting)
        if raw is None:
            return setting != PERSISTENCE_SETTING
        return bool(json.loads(raw))

    def set(self, setting: str, enabled: bool, now: float | None = None) -> dict:
        """编排：写 SQLite → runtime action → 失败回滚设置值并调用动作回滚"""
        if setting not in ToggleSettings and setting != PERSISTENCE_SETTING:
            raise ValueError(f"未知隐私设置: {setting}")
        ts = time.time() if now is None else now
        previous = self.get(setting)
        self._store.set_setting(_SETTING_PREFIX + setting, json.dumps(bool(enabled)), now=ts)
        try:
            self._actions.apply(setting, bool(enabled))
        except Exception:  # noqa: BLE001 - 动作失败回滚设置值
            self._store.set_setting(_SETTING_PREFIX + setting, json.dumps(previous), now=ts)
            try:
                self._actions.rollback(setting, previous)
            except Exception:  # noqa: BLE001 - 回滚动作失败不掩盖原始失败
                pass
            return {
                "applied_at": ts,
                "effective_value": previous,
                "action_result": "failed",
            }
        return {
            "applied_at": ts,
            "effective_value": bool(enabled),
            "action_result": "ok",
        }
