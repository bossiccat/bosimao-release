"""SSE 解析器单测（backend-llama-client-spec §3 验收）"""
from __future__ import annotations

import httpx
import pytest

from app.engine.sse import SseProtocolError, iter_sse_chunks, parse_sse_line


class TestParseSseLine:
    def test_standard_delta(self):
        ev = parse_sse_line(
            'data: {"content":"你好","stop":false,"round_idx":0,'
            '"is_listen":false,"end_of_turn":false}'
        )
        assert ev is not None
        assert ev.kind == "delta"
        assert ev.content == "你好"
        assert ev.stop is False
        assert ev.round_idx == 0

    def test_delta_stop_true(self):
        ev = parse_sse_line('data: {"content":"x","stop":true}')
        assert ev is not None
        assert ev.kind == "delta"
        assert ev.stop is True

    def test_done(self):
        ev = parse_sse_line("data: [DONE]")
        assert ev is not None
        assert ev.kind == "done"

    def test_empty_line_none(self):
        assert parse_sse_line("") is None
        assert parse_sse_line("\r\n") is None
        assert parse_sse_line("   ") is None

    def test_comment_line_none(self):
        assert parse_sse_line(": this is a comment") is None

    def test_event_name_or_other_line_none(self):
        assert parse_sse_line("event: delta") is None
        assert parse_sse_line("hello world") is None

    def test_error_payload_classified(self):
        ev = parse_sse_line('data: {"error":"model overloaded"}')
        assert ev is not None
        assert ev.kind == "error"
        assert ev.content == "model overloaded"

    def test_malformed_json_raises(self):
        with pytest.raises(SseProtocolError):
            parse_sse_line("data: {not json")

    def test_metadata_frame_skipped(self):
        # 实测（POC-001 硬件路径）：服务端会发 kv_cache_length 等元数据帧，须跳过
        assert parse_sse_line('data: {"kv_cache_length":116}') is None
        assert parse_sse_line('data: {"some_meta": 1}') is None


class TestIterSseChunks:
    @staticmethod
    def _resp(body: str) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    async def _collect(self, body: str) -> list:
        return [ev async for ev in iter_sse_chunks(self._resp(body))]

    @pytest.mark.asyncio
    async def test_delta_then_done(self):
        body = (
            'data: {"content":"a","stop":false}\n'
            'data: {"content":"b","stop":true}\n'
            "data: [DONE]\n"
        )
        events = await self._collect(body)
        assert [e.kind for e in events] == ["delta", "delta", "done"]
        assert "".join(e.content for e in events if e.kind == "delta") == "ab"

    @pytest.mark.asyncio
    async def test_multiline_data_joined(self):
        # 事件被拆成多行 data:（token 间换行，拼接后是合法 JSON）
        body = 'data: {"content":\ndata: "hello"}\ndata: [DONE]\n'
        events = await self._collect(body)
        assert len(events) == 2
        assert events[0].kind == "delta"
        assert events[0].content == "hello"
        assert events[1].kind == "done"

    @pytest.mark.asyncio
    async def test_empty_and_comment_lines_skipped(self):
        body = '\n: comment\ndata: {"content":"x"}\n\n'
        events = await self._collect(body)
        assert len(events) == 1
        assert events[0].content == "x"

    @pytest.mark.asyncio
    async def test_metadata_frames_skipped(self):
        body = (
            'data: {"kv_cache_length":116}\n'
            'data: {"content":"a"}\n'
            'data: {"kv_cache_length":117}\n'
            "data: [DONE]\n"
        )
        events = await self._collect(body)
        assert [e.kind for e in events] == ["delta", "done"]
        assert events[0].content == "a"

    @pytest.mark.asyncio
    async def test_malformed_raises_not_skipped(self):
        body = 'data: {"content":"ok"}\ndata: {broken\n'
        with pytest.raises(SseProtocolError):
            await self._collect(body)

    @pytest.mark.asyncio
    async def test_done_only(self):
        events = await self._collect("data: [DONE]\n")
        assert [e.kind for e in events] == ["done"]

    @pytest.mark.asyncio
    async def test_no_trailing_newline(self):
        body = 'data: {"content":"z"}'
        events = await self._collect(body)
        assert len(events) == 1
        assert events[0].content == "z"
