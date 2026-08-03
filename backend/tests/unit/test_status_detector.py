"""status_detector 单元测试（含变异定向加固）

覆盖：
- 3 帧边界（2/3/4 帧）
- 120s 超时边界（119/120s）
- 恢复清除逻辑
- 跑偏判定
- 未知帧清零候选（防误报）
"""
from __future__ import annotations

import time

import pytest

from app.config import DetectionConfig
from app.core.state import AgentState, SessionSnapshot
from app.engine.status_detector import detect_status


def make_cfg(**kw) -> DetectionConfig:
    defaults = dict(stuck_frame_threshold=3, stuck_timeout_seconds=120, off_track_frame_threshold=2)
    defaults.update(kw)
    return DetectionConfig(**defaults)


def make_snap(app_id="codex", app_name="Codex") -> SessionSnapshot:
    return SessionSnapshot(app_id=app_id, app_name=app_name)


def feed_frames(snap, states, cfg, base_time):
    """按 states 序列喂帧，返回 (结果序列, 快照)"""
    results = []
    t = base_time
    for s in states:
        results.append(detect_status(snap, s, cfg, now=t))
        t += 30  # 每帧间隔 30s
    return results, snap


class TestStuckFrameBoundary:
    """变异靶：把 stuck_frame_threshold 误写成 2 或 4 时，必须有测试变红"""

    def test_2_frames_not_trigger(self):
        """2 帧 STUCK 不触发（阈值 3）"""
        snap = make_snap()
        results, _ = feed_frames(
            snap,
            [AgentState.STUCK, AgentState.STUCK],
            make_cfg(),
            base_time=1_000,
        )
        assert all(not r.triggered for r in results), "2 帧不应触发"

    def test_3_frames_with_timeout_triggers(self):
        """3 帧 STUCK + 时间达标 → 触发"""
        snap = make_snap()
        # 帧1(1000) 帧2(1030) 帧3(1060)，stuck_since=1000，到帧3时 elapsed=60 < 120 → 不触发
        results, _ = feed_frames(
            snap,
            [AgentState.STUCK, AgentState.STUCK, AgentState.STUCK],
            make_cfg(),
            base_time=1_000,
        )
        assert not results[-1].triggered, "elapsed=60s 不应触发"

    def test_3_frames_long_timeout_triggers(self):
        """3 帧 STUCK + 超 120s → 触发（时间条件达标）"""
        snap = make_snap()
        t = 1_000
        for s in [AgentState.STUCK, AgentState.STUCK, AgentState.STUCK]:
            detect_status(snap, s, make_cfg(), now=t)
            t += 60  # 每帧间隔 60s，帧3 时 elapsed=120s
        final = detect_status(snap, AgentState.STUCK, make_cfg(), now=t)
        assert final.triggered, "elapsed=120s 应触发"
        assert final.alert_level == 4
        assert final.reason == "stuck_timeout"

    def test_timeout_119s_not_trigger(self):
        """变异靶：超时阈值 119s 误写成 120s 边界，119s 不触发"""
        snap = make_snap()
        detect_status(snap, AgentState.STUCK, make_cfg(), now=1_000)
        detect_status(snap, AgentState.STUCK, make_cfg(), now=1_059)  # +59
        r = detect_status(snap, AgentState.STUCK, make_cfg(), now=1_119)  # +60 → elapsed=119
        assert not r.triggered, "elapsed=119s 不应触发（边界内）"

    def test_2_frames_elapsed_120_not_trigger(self):
        """变异靶：帧阈值 3→2 时，2 帧 + elapsed≥120 会误触发；阈值 3 必须不触发

        设计要点：构造 [STUCK@1000, STUCK@1120] 两帧序列（帧间隔 120s）。
        若实现被变异为有效阈值 2，则第 2 帧即满足 stuck_frames>=2 且
        elapsed=120>=120 → 误触发 → 本断言变红（变异被杀）。
        正确实现（阈值 3）下 stuck_frames=2 < 3 → 不触发。
        """
        snap = make_snap()
        detect_status(snap, AgentState.STUCK, make_cfg(), now=1_000)
        r = detect_status(snap, AgentState.STUCK, make_cfg(), now=1_120)  # 第2帧 elapsed=120
        assert not r.triggered, "帧阈值 3：仅 2 帧即使超 120s 也不应触发"
        assert r.reason != "stuck_timeout"

    def test_3_frames_exact_120_triggers(self):
        """变异靶：帧阈值 3→4 或超时 120→121 时，3 帧 + 恰好 120s 不应触发；正确实现必须触发

        设计要点：只喂 3 帧 [STUCK@1000, STUCK@1060, STUCK@1120]，第 3 帧 elapsed 恰为 120s。
        - 若帧阈值被变异为 4：第 3 帧 stuck_frames=3 < 4 → 不触发 → 本断言变红（变异被杀）
        - 若超时被变异为 121：elapsed=120 < 121 → 不触发 → 本断言变红（变异被杀）
        """
        snap = make_snap()
        detect_status(snap, AgentState.STUCK, make_cfg(), now=1_000)
        detect_status(snap, AgentState.STUCK, make_cfg(), now=1_060)
        r = detect_status(snap, AgentState.STUCK, make_cfg(), now=1_120)  # 第3帧 elapsed=120
        assert r.triggered, "3 帧 + elapsed=120s 必须触发"
        assert r.alert_level == 4
        assert r.reason == "stuck_timeout"


class TestRecovery:
    def test_recovery_clears_state(self):
        """卡住后恢复 progress → 清除候选 + 触发恢复通知"""
        snap = make_snap()
        t = 1_000
        detect_status(snap, AgentState.STUCK, make_cfg(), now=t)
        detect_status(snap, AgentState.STUCK, make_cfg(), now=t + 130)
        r = detect_status(snap, AgentState.PROGRESS, make_cfg(), now=t + 160)
        assert r.triggered and r.reason == "recovered"
        assert snap.stuck_frames == 0
        assert snap.stuck_since is None
        assert snap.state == AgentState.PROGRESS

    def test_progress_resets_stuck_candidate(self):
        """progress 帧清零卡住候选（即使后续又 stuck，stuck_since 重新计）"""
        snap = make_snap()
        t = 1_000
        detect_status(snap, AgentState.STUCK, make_cfg(), now=t)
        detect_status(snap, AgentState.PROGRESS, make_cfg(), now=t + 30)
        assert snap.stuck_frames == 0
        assert snap.stuck_since is None


class TestOffTrack:
    def test_off_track_threshold(self):
        """跑偏连续 2 帧触发（阈值 2）"""
        snap = make_snap()
        r1 = detect_status(snap, AgentState.OFF_TRACK, make_cfg(), now=1_000)
        r2 = detect_status(snap, AgentState.OFF_TRACK, make_cfg(), now=1_030)
        assert not r1.triggered
        assert r2.triggered and r2.reason == "off_track" and r2.alert_level == 3

    def test_off_track_single_frame_not_trigger(self):
        """单帧跑偏不触发（阈值 2）"""
        snap = make_snap()
        r = detect_status(snap, AgentState.OFF_TRACK, make_cfg(), now=1_000)
        assert not r.triggered


class TestUnknownFrame:
    def test_unknown_resets_candidates(self):
        """未知帧清零候选（防误报：捕获失败不应累积卡住）"""
        snap = make_snap()
        detect_status(snap, AgentState.STUCK, make_cfg(), now=1_000)
        detect_status(snap, AgentState.UNKNOWN, make_cfg(), now=1_030)
        assert snap.stuck_frames == 0
        assert snap.stuck_since is None


class TestStateTransition:
    def test_state_updated_correctly(self):
        """快照状态随帧更新"""
        snap = make_snap()
        detect_status(snap, AgentState.PROGRESS, make_cfg(), now=1_000)
        assert snap.state == AgentState.PROGRESS
        detect_status(snap, AgentState.OFF_TRACK, make_cfg(), now=1_030)
        assert snap.state == AgentState.OFF_TRACK
        assert snap.last_state == AgentState.PROGRESS
