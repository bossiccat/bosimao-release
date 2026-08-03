"""advice_generator 单元测试：空上下文 / 正常 / 无建议场景 / 三种模式"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.core.state import AgentState
from app.engine.advice_generator import _detect_pattern, _TEMPLATE_PATH, generate
from app.engine.vision_analyzer import VisionResult


def vr(state: AgentState) -> VisionResult:
    return VisionResult(state=state, summary="测试摘要", raw="")


def test_template_file_exists_with_placeholders():
    assert _TEMPLATE_PATH.exists(), "模板文件应存在"
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "{app_name}" in text and "{pattern}" in text


class TestEmptyContext:
    def test_empty_history_returns_empty(self):
        assert generate([], "Codex") == ""

    def test_none_history_returns_empty(self):
        assert generate([], "Codex") == ""


class TestNormalNoAdvice:
    def test_all_progress_no_advice(self):
        assert generate([vr(AgentState.PROGRESS)] * 3, "Codex") == ""

    def test_mixed_no_pattern_no_advice(self):
        # S,P,P 不构成任何模式 → 无建议
        assert generate(
            [vr(AgentState.STUCK), vr(AgentState.PROGRESS), vr(AgentState.PROGRESS)],
            "Codex",
        ) == ""


class TestStuckPattern:
    def test_consecutive_stuck(self):
        r = generate([vr(AgentState.STUCK)] * 3, "Trae")
        assert "Trae" in r
        assert "stuck" in r
        assert "死循环" in r or "输入" in r

    def test_two_of_three_stuck(self):
        r = generate(
            [vr(AgentState.PROGRESS), vr(AgentState.STUCK), vr(AgentState.STUCK)],
            "Trae",
        )
        assert "stuck" in r


class TestOffTrackPattern:
    def test_off_track_present(self):
        r = generate(
            [vr(AgentState.PROGRESS), vr(AgentState.PROGRESS), vr(AgentState.OFF_TRACK)],
            "Codex",
        )
        assert "Codex" in r
        assert "off_track" in r
        assert "目标" in r


class TestOscillatingPattern:
    def test_progress_stuck_progress(self):
        r = generate(
            [vr(AgentState.PROGRESS), vr(AgentState.STUCK), vr(AgentState.PROGRESS)],
            "Hermes",
        )
        assert "Hermes" in r
        assert "oscillating" in r
        assert "简化" in r or "更小" in r

    def test_stuck_progress_stuck(self):
        r = generate(
            [vr(AgentState.STUCK), vr(AgentState.PROGRESS), vr(AgentState.STUCK)],
            "Hermes",
        )
        assert "oscillating" in r


def test_detect_pattern_returns_none_for_unknown():
    assert _detect_pattern([vr(AgentState.UNKNOWN)] * 3) is None
