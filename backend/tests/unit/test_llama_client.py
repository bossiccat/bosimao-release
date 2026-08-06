"""LlamaOmniClient SSE 消费单测（mock httpx 流式响应）

覆盖：SSE 拼接返回纯文本 / 错误三分类 / 网络错误整轮重试 1 次。
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.engine.llama_omni_client import (
    LlamaOmniClient,
    ModelError,
    ModelServerError,
    SseProtocolError,
)
from app.engine.vision_analyzer import parse_vision_output

SSE_JSON = '{"state":"progress","summary":"正在生成代码"}'
SSE_BODY = (
    'data: {"content":"<think>分析中</think>","stop":false,"round_idx":0}\n'
    f'data: {{"content":"{SSE_JSON.replace(chr(34), chr(92)+chr(34))}","stop":true}}\n'
    "data: [DONE]\n"
)


def _settings() -> Settings:
    return Settings(model_server_host="127.0.0.1", model_server_port=19080)


def _image(tmp_path: Path) -> Path:
    p = tmp_path / "shot.png"
    p.write_bytes(b"fake-png")
    return p


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/stream/omni_init":
        return httpx.Response(200, json={"round_idx": 0})
    if path == "/v1/stream/prefill":
        return httpx.Response(200, json={"ok": True})
    if path == "/v1/stream/decode":
        return httpx.Response(
            200, content=SSE_BODY.encode(), headers={"content-type": "text/event-stream"}
        )
    return httpx.Response(404)


class TestSseConsumption:
    @pytest.mark.asyncio
    async def test_vision_analyze_returns_joined_text(self, tmp_path):
        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(_ok_handler))
        text = await client.vision_analyze(_image(tmp_path), "判定状态")
        assert "<think>分析中</think>" in text
        assert '"state":"progress"' in text
        assert "data:" not in text  # 不是原始 SSE

    @pytest.mark.asyncio
    async def test_vision_analyze_feeds_parser(self, tmp_path):
        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(_ok_handler))
        text = await client.vision_analyze(_image(tmp_path), "判定状态")
        result = parse_vision_output(text)
        assert result.state.value == "progress"
        assert "正在生成代码" in result.summary

    @pytest.mark.asyncio
    async def test_chat_returns_text(self):
        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(_ok_handler))
        text = await client.chat("你好", max_tokens=64)
        assert "<think>分析中</think>" in text

    @pytest.mark.asyncio
    async def test_missing_image_raises_model_error(self, tmp_path):
        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(_ok_handler))
        with pytest.raises(ModelError):
            await client.vision_analyze(tmp_path / "not-exist.png", "判定状态")


class TestErrorClassification:
    @pytest.mark.asyncio
    async def test_network_error_retries_once_then_ok(self, tmp_path):
        calls = {"decode": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/stream/omni_init":
                return httpx.Response(200, json={"round_idx": 0})
            if path == "/v1/stream/prefill":
                return httpx.Response(200, json={"ok": True})
            if path == "/v1/stream/decode":
                calls["decode"] += 1
                if calls["decode"] == 1:
                    raise httpx.ConnectError("connection refused")
                return httpx.Response(
                    200,
                    content=SSE_BODY.encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(404)

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(flaky))
        text = await client.vision_analyze(_image(tmp_path), "判定状态")
        assert calls["decode"] == 2  # 首次失败 + 重试 1 次
        assert '"state":"progress"' in text

    @pytest.mark.asyncio
    async def test_network_error_exhausted_raises_server_error(self, tmp_path):
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(down))
        with pytest.raises(ModelServerError):
            await client.vision_analyze(_image(tmp_path), "判定状态")

    @pytest.mark.asyncio
    async def test_http_4xx_raises_model_error_not_retried(self, tmp_path):
        calls = {"prefill": 0}

        def bad(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/stream/omni_init":
                return httpx.Response(200, json={"round_idx": 0})
            if request.url.path == "/v1/stream/prefill":
                calls["prefill"] += 1
                return httpx.Response(400, json={"error": "bad request"})
            return httpx.Response(404)

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(bad))
        with pytest.raises(ModelError):
            await client.vision_analyze(_image(tmp_path), "判定状态")
        assert calls["prefill"] == 1  # 模型类错误不重试

    @pytest.mark.asyncio
    async def test_malformed_sse_raises_protocol_error(self, tmp_path):
        def bad_sse(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/stream/omni_init":
                return httpx.Response(200, json={"round_idx": 0})
            if request.url.path == "/v1/stream/prefill":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/stream/decode":
                return httpx.Response(
                    200,
                    content=b'data: {broken\n',
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(404)

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(bad_sse))
        with pytest.raises(SseProtocolError):
            await client.vision_analyze(_image(tmp_path), "判定状态")

    @pytest.mark.asyncio
    async def test_missing_done_raises_protocol_error(self, tmp_path):
        def no_done(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/stream/omni_init":
                return httpx.Response(200, json={"round_idx": 0})
            if request.url.path == "/v1/stream/prefill":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/stream/decode":
                return httpx.Response(
                    200,
                    content=b'data: {"content":"x","stop":false}\n',
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(404)

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(no_done))
        with pytest.raises(SseProtocolError):
            await client.vision_analyze(_image(tmp_path), "判定状态")

    @pytest.mark.asyncio
    async def test_sse_error_event_raises_model_error(self, tmp_path):
        def err_event(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/stream/omni_init":
                return httpx.Response(200, json={"round_idx": 0})
            if request.url.path == "/v1/stream/prefill":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/stream/decode":
                return httpx.Response(
                    200,
                    content=b'data: {"error":"model overloaded"}\n',
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(404)

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(err_event))
        with pytest.raises(ModelError):
            await client.vision_analyze(_image(tmp_path), "判定状态")

    @pytest.mark.asyncio
    async def test_non_sse_content_type_raises_protocol_error(self, tmp_path):
        def json_response(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/stream/omni_init":
                return httpx.Response(200, json={"round_idx": 0})
            if request.url.path == "/v1/stream/prefill":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/v1/stream/decode":
                return httpx.Response(
                    200,
                    content=b'{"content":"x"}',
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(404)

        client = LlamaOmniClient(_settings(), transport=httpx.MockTransport(json_response))
        with pytest.raises(SseProtocolError):
            await client.vision_analyze(_image(tmp_path), "判定状态")


class TestConfigDefaults:
    def test_ctx_size_is_4096(self):
        assert _settings().model_ctx_size == 4096  # B1 结论：8192 爆显存

    def test_timeout_defaults(self):
        s = _settings()
        assert s.model_connect_timeout_s == 10.0
        assert s.model_prefill_timeout_s == 60.0
        assert s.model_stream_idle_timeout_s == 20.0
        assert s.model_round_timeout_s == 120.0
        assert s.model_retry_count == 1
        assert s.model_retry_backoff_s == 1.0
