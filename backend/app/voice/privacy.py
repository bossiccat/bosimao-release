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


class OrchestratorHolder:
    """late-bound orchestrator 间接引用（对齐 routes_capture.orchestrator 模式）。

    PrivacyService 在模块装配期（_build_secured_session_router）创建，而
    Orchestrator 在 main.lifespan 内创建，二者生命周期不同。通过本 holder 解耦，
    lifespan 里调用 privacy_runtime.bind(orchestrator) 完成绑定。
    """

    def __init__(self) -> None:
        self._orchestrator = None

    def bind(self, orchestrator) -> None:
        self._orchestrator = orchestrator

    def get(self):
        return self._orchestrator


# 模块级 holder（PrivacyRuntimeActions.apply/rollback 经此取 Orchestrator）
privacy_runtime = OrchestratorHolder()


class PrivacyRuntimeActions(RuntimeActions):
    """真实运行时动作适配器（生产装配；测试仍用 FakeRuntimeActions）。

    - desktop_capture_enabled → late-bound Orchestrator.set_desktop_capture
      （bind 前 apply 抛异常 → PrivacyService.set 自动回滚，不静默成功）
    - cloud_processing_enabled → no-op（强制靠读时门禁 D1，无进程内状态副作用）
    - microphone_enabled / background_conversation_enabled → no-op（采集 owner 在 Android）
    """

    def apply(self, setting: str, enabled: bool) -> None:
        if setting == "desktop_capture_enabled":
            orchestrator = privacy_runtime.get()
            if orchestrator is None:
                raise RuntimeError("orchestrator 未绑定：桌面捕获动作不可用")
            orchestrator.set_desktop_capture(bool(enabled))

    def rollback(self, setting: str, previous: bool) -> None:
        if setting == "desktop_capture_enabled":
            orchestrator = privacy_runtime.get()
            if orchestrator is not None:
                orchestrator.set_desktop_capture(bool(previous))


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

    def set(self, setting: str, enabled: bool, now: float | None = None,
            actor: str = "local") -> dict:
        """编排：写 SQLite → runtime action → 失败回滚设置值并调用动作回滚。

        每次切换强制写审计（ADR-021 D3）；actor 由路由层透传（无 owner 记 "local"）。
        回滚路径也写一条 result="failed" 审计。
        """
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
            self._write_audit(setting, previous, bool(enabled), actor, "failed", ts)
            return {
                "applied_at": ts,
                "effective_value": previous,
                "action_result": "failed",
            }
        self._write_audit(setting, previous, bool(enabled), actor, "ok", ts)
        return {
            "applied_at": ts,
            "effective_value": bool(enabled),
            "action_result": "ok",
        }

    def _write_audit(self, setting: str, old: bool, new: bool, actor: str,
                     result: str, ts: float) -> None:
        """脱敏审计：old/new 纯布尔 + actor 纯文本，不含敏感明文"""
        self._store.write_audit(
            action="privacy.toggle",
            subject_type="setting",
            subject_id=setting,
            result=result,
            metadata_redacted_json={"old": old, "new": new, "actor": actor},
            now=ts,
        )
