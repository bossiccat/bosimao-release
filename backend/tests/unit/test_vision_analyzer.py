"""vision_analyzer 解析测试（容错：markdown 代码块 / 损坏 JSON）"""
from __future__ import annotations

import pytest

from app.core.state import AgentState
from app.engine.vision_analyzer import parse_vision_output


class TestParseClean:
    def test_pure_json(self):
        r = parse_vision_output('{"state": "progress", "summary": "正在生成代码"}')
        assert r.state == AgentState.PROGRESS
        assert r.summary == "正在生成代码"

    def test_markdown_codeblock(self):
        r = parse_vision_output('```json\n{"state": "stuck", "summary": "等待输入"}\n```')
        assert r.state == AgentState.STUCK

    def test_states_mapping(self):
        assert parse_vision_output('{"state":"off_track","summary":"x"}').state == AgentState.OFF_TRACK
        assert parse_vision_output('{"state":"unknown","summary":"x"}').state == AgentState.UNKNOWN
        # 大小写容错
        assert parse_vision_output('{"state":"Progress","summary":"x"}').state == AgentState.PROGRESS


class TestParseGarbage:
    def test_invalid_json_fallback_unknown(self):
        r = parse_vision_output("not json at all")
        assert r.state == AgentState.UNKNOWN

    def test_embedded_json_extracted(self):
        r = parse_vision_output('前缀文本 {"state": "progress", "summary": "ok"} 后缀')
        assert r.state == AgentState.PROGRESS

    def test_empty_input(self):
        r = parse_vision_output("")
        assert r.state == AgentState.UNKNOWN
