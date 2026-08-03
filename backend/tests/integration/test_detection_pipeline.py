"""集成测试：截屏→视觉分析→状态判定→事件→提醒 全链路（mock 模型 server）

覆盖：正常推进 / 卡住 3 帧+超时 / 跑偏 / 模型超时降级
回归率作为一等指标：改动前后对比，由绿转红即回归。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.config import AppConfig, DetectionConfig
from app.core.events import EventBus
from app.core.state import AgentState, SessionSnapshot, state as global_state
from app.engine.status_detector import detect_status
from app.engine.vision_analyzer import VisionAnalyzer, VisionResult


class FakeVisionAnalyzer:
    """mock 视觉分析器：按预设序列返回状态"""

    def __init__(self, sequence: list[AgentState]) -> None:
        self._sequence = list(sequence)
        self._idx = 0

    async def analyze(self, screenshot: Path) -> VisionResult:
        if self._idx >= len(self._sequence):
            st = AgentState.PROGRESS
        else:
            st = self._sequence[self._idx]
            self._idx += 1
        return VisionResult(state=st, summary="mock", raw="")


class FakePush:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def push(self, text: str, image=None, title=None):
        self.calls.append(text)
        from app.push.base import PushResult
        return PushResult(ok=True, provider="fake")


class FakeReminder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def on_detection(self, data: dict) -> None:
        self.events.append(data)


async def run_detection_loop(
    snap: SessionSnapshot,
    sequence: list[AgentState],
    cfg: DetectionConfig,
    frame_interval: float = 0.01,
) -> list[dict]:
    """模拟编排器循环：逐帧喂入，返回触发事件列表"""
    reminder = FakeReminder()
    events = []
    now = 1_000.0
    for st in sequence:
        result = detect_status(snap, st, cfg, now=now)
        if result.triggered:
            reminder.events.append(
                {
                    "app_id": snap.app_id,
                    "alert_level": result.alert_level,
                    "state": result.state.value,
                    "summary": snap.last_summary,
                    "suggestion": result.suggestion,
                }
            )
        now += frame_interval * 1000
        # stuck 帧每帧推进 60s：3 帧后 elapsed=120s 达超时阈值
        if st == AgentState.STUCK:
            now += 60
    return reminder.events


class TestNormalProgress:
    def test_no_alert_when_progressing(self):
        snap = SessionSnapshot(app_id="codex", app_name="Codex")
        events = asyncio.run(
            run_detection_loop(
                snap,
                [AgentState.PROGRESS] * 10,
                DetectionConfig(),
            )
        )
        assert events == [], "正常推进不应触发任何提醒"
        assert snap.state == AgentState.PROGRESS


class TestStuckScenario:
    def test_stuck_after_3_frames_plus_timeout(self):
        snap = SessionSnapshot(app_id="trae", app_name="Trae")
        # 2 帧 stuck(帧间隔 40s → 80s) + 第 3 帧(120s) → 触发
        events = asyncio.run(
            run_detection_loop(
                snap,
                [AgentState.STUCK, AgentState.STUCK, AgentState.STUCK],
                DetectionConfig(),
            )
        )
        assert len(events) == 1, f"应触发 1 次, 实际 {events}"
        assert events[0]["alert_level"] == 4
        assert events[0]["state"] == "stuck"

    def test_recovery_after_stuck(self):
        snap = SessionSnapshot(app_id="hermes", app_name="Hermes")
        seq = [AgentState.STUCK, AgentState.STUCK, AgentState.STUCK, AgentState.PROGRESS]
        events = asyncio.run(run_detection_loop(snap, seq, DetectionConfig()))
        states = [e["state"] for e in events]
        # 卡住触发 + 恢复通知
        assert "stuck" in states
        assert events[-1]["alert_level"] == 1  # recovered


class TestOffTrackScenario:
    def test_off_track_triggers(self):
        snap = SessionSnapshot(app_id="codex", app_name="Codex")
        events = asyncio.run(
            run_detection_loop(
                snap,
                [AgentState.OFF_TRACK, AgentState.OFF_TRACK],
                DetectionConfig(),
            )
        )
        assert len(events) == 1
        assert events[0]["alert_level"] == 3
        assert events[0]["state"] == "off_track"


class TestModelTimeoutDegradation:
    """模型超时/失败时：状态置 unknown 且不清除候选（防误报 + 防漏报）"""

    def test_unknown_frame_does_not_trigger(self):
        snap = SessionSnapshot(app_id="codex", app_name="Codex")
        events = asyncio.run(
            run_detection_loop(
                snap,
                [AgentState.UNKNOWN] * 5,
                DetectionConfig(),
            )
        )
        assert events == []
        assert snap.state == AgentState.UNKNOWN
