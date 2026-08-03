"""状态判定引擎：3 帧不变 + 120s 超时 → 触发卡住/跑偏提醒

纯函数设计（不依赖 IO），可独立单测（含变异定向加固用例见 tests/unit/）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import DetectionConfig
from ..core.state import AgentState, SessionSnapshot


@dataclass
class DetectionResult:
    """一次判定结果"""

    app_id: str
    triggered: bool = False           # 是否触发提醒
    alert_level: int = 0              # 1=状态点 2=微动 3=浮起 4=语音+推送
    reason: str = ""                  # 触发原因（stuck_timeout / off_track）
    state: AgentState = AgentState.UNKNOWN
    suggestion: str = ""


def _evaluate_frame(
    snapshot: SessionSnapshot, frame_state: AgentState, now: float
) -> AgentState:
    """单帧评估：根据视觉判定三态，先修正快照状态（不触发提醒）"""
    snapshot.last_state = snapshot.state

    if frame_state == AgentState.PROGRESS:
        # 有进展：清除卡住/跑偏候选
        snapshot.stuck_frames = 0
        snapshot.off_track_frames = 0
        snapshot.stuck_since = None
        snapshot.state = AgentState.PROGRESS
    elif frame_state == AgentState.STUCK:
        snapshot.stuck_frames += 1
        snapshot.off_track_frames = 0
        if snapshot.state != AgentState.STUCK:
            snapshot.stuck_since = now
        snapshot.state = AgentState.STUCK
    elif frame_state == AgentState.OFF_TRACK:
        snapshot.off_track_frames += 1
        snapshot.stuck_frames = 0
        snapshot.stuck_since = None
        snapshot.state = AgentState.OFF_TRACK
    elif frame_state == AgentState.UNKNOWN:
        # 无法判定帧：不改变状态，但清零卡住候选（避免误报）
        snapshot.stuck_frames = 0
        snapshot.off_track_frames = 0
        snapshot.stuck_since = None
        snapshot.state = AgentState.UNKNOWN
    return snapshot.state


def detect_status(
    snapshot: SessionSnapshot,
    frame_state: AgentState,
    cfg: DetectionConfig,
    now: float | None = None,
) -> DetectionResult:
    """主判定入口（纯函数，now 可注入便于测试）。

    触发条件（双条件防误判）：
    - 卡住：连续 stuck_frame_threshold 帧为 STUCK 且持续 >= stuck_timeout_seconds
    - 跑偏：连续 off_track_frame_threshold 帧为 OFF_TRACK
    """
    now = now or time.time()
    result = DetectionResult(app_id=snapshot.app_id, state=frame_state)

    state_after = _evaluate_frame(snapshot, frame_state, now)

    # 卡住判定：帧数条件 + 时间条件（双条件）
    if state_after == AgentState.STUCK:
        if snapshot.stuck_frames >= cfg.stuck_frame_threshold:
            if snapshot.stuck_since is None:
                snapshot.stuck_since = now
            elapsed = now - snapshot.stuck_since
            if elapsed >= cfg.stuck_timeout_seconds + 1:
                result.triggered = True
                result.alert_level = 4
                result.reason = "stuck_timeout"
                result.suggestion = (
                    f"{snapshot.app_name} 已卡住 {int(elapsed)}s，建议检查是否等待输入或死循环。"
                )

    # 跑偏判定：连续帧数条件（内容级，缓冲较小）
    elif state_after == AgentState.OFF_TRACK:
        if snapshot.off_track_frames >= cfg.off_track_frame_threshold:
            result.triggered = True
            result.alert_level = 3
            result.reason = "off_track"
            result.suggestion = (
                f"{snapshot.app_name} 可能偏离目标，建议核对最近改动方向。"
            )

    # 恢复判定：从异常态回到 progress → 通知恢复（由调用方订阅事件）
    if (
        snapshot.last_state in (AgentState.STUCK, AgentState.OFF_TRACK)
        and state_after == AgentState.PROGRESS
    ):
        result.triggered = True
        result.alert_level = 1
        result.reason = "recovered"
        result.suggestion = f"{snapshot.app_name} 已恢复进展。"

    snapshot.alert_level = result.alert_level
    return result
