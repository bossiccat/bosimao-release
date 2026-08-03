"""全局状态：每被监控会话快照 + 连续不变帧计数"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentState(str, Enum):
    """视觉判定三态 + 辅助态"""

    PROGRESS = "progress"     # 有进展
    STUCK = "stuck"           # 卡住
    OFF_TRACK = "off_track"   # 跑偏
    UNKNOWN = "unknown"       # 无法判定（窗口未找到/捕获失败）
    OFFLINE = "offline"       # 应用未运行


@dataclass
class SessionSnapshot:
    """单个被监控会话的最新状态快照"""

    app_id: str
    app_name: str
    window_found: bool = False
    capture_mode: str = "none"          # wgc / dxgi / none
    state: AgentState = AgentState.UNKNOWN
    last_state: AgentState = AgentState.UNKNOWN
    state_changed_at: float = field(default_factory=time.time)
    stuck_frames: int = 0               # 连续不变帧数（卡住判定用）
    off_track_frames: int = 0           # 连续跑偏帧数
    stuck_since: float | None = None    # 进入卡住候选的时间戳
    last_summary: str = ""
    last_suggestion: str = ""
    last_frame_at: float = 0.0
    frame_count: int = 0
    last_analysis_ms: int = 0
    alert_level: int = 0                # 当前渐进打扰级别 0-4

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "app_name": self.app_name,
            "window_found": self.window_found,
            "capture_mode": self.capture_mode,
            "state": self.state.value,
            "last_state": self.last_state.value,
            "state_changed_at": self.state_changed_at,
            "stuck_frames": self.stuck_frames,
            "last_summary": self.last_summary,
            "last_suggestion": self.last_suggestion,
            "last_frame_at": self.last_frame_at,
            "frame_count": self.frame_count,
            "last_analysis_ms": self.last_analysis_ms,
            "alert_level": self.alert_level,
        }


class GlobalState:
    """全局状态容器（单例，由 orchestrator 维护）"""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionSnapshot] = {}
        self.model_loaded = False
        self.model_vram_mb = 0
        self.inference_busy = False
        self.pet_state = "monitoring"   # idle/monitoring/listening/thinking/speaking/alerting

    def get_or_create(self, app_id: str, app_name: str) -> SessionSnapshot:
        if app_id not in self._sessions:
            self._sessions[app_id] = SessionSnapshot(app_id=app_id, app_name=app_name)
        return self._sessions[app_id]

    def get(self, app_id: str) -> SessionSnapshot | None:
        return self._sessions.get(app_id)

    def all(self) -> list[SessionSnapshot]:
        return list(self._sessions.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": {
                "model_loaded": self.model_loaded,
                "vram_mb": self.model_vram_mb,
                "inference_busy": self.inference_busy,
            },
            "sessions": [s.to_dict() for s in self.all()],
            "pet_state": self.pet_state,
            "updated_at": time.time(),
        }


# 全局唯一状态
state = GlobalState()
