"""DeepSeekClient 单测（mock httpx.MockTransport）

覆盖：错误四分类 / 重试次数与退避 / 熔断 / trust_env=False / 非流式 JSON。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.brain.deepseek_client import (
    DeepSeekAuthError,
    DeepSeekClient,
    DeepSeekHttpError,
    DeepSeekNetworkError,
    DeepSeekProtocolError,
    DeepSeekRateLimitError,
    DeepSeekTimeoutError,
)
from app.brain.schemas import BrainTask
from app.config import BrainConfig, DeepSeekConfig, Settings

OK_BODY = {
    "choices": [{"message": {"content": "{\"subtasks\": []}"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _cfg() -> BrainConfig:
    # 退避归零，测试瞬时完成
    return BrainConfig(
        deepseek=DeepSeekConfig(network_backoff_s=[0.0, 0.0], http_backoff_s=0.0)
    )


def _client(handler, key: str = "sk-test") -> DeepSeekClient:
    settings = Settings(deepseek_api_key=key)
    return DeepSeekClient(settings, _cfg(), transport=httpx.MockTransport(handler))


def _ok(handler, calls: dict) -> None:
    calls["n"] = calls.get("n", 0) + 1


class TestChat:
    @pytest.mark.asyncio
    async def test_chat_returns_content(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["stream"] is False
            assert body["model"] == "deepseek-v4-flash"
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        text = await client.chat([{"role": "user", "content": "hi"}], max_tokens=64)
        assert text == '{"subtasks": []}'

    @pytest.mark.asyncio
    async def test_chat_json_uses_response_format(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        obj = await client.chat_json([{"role": "user", "content": "x"}], max_tokens=64)
        assert obj == {"subtasks": []}

    @pytest.mark.asyncio
    async def test_trust_env_false(self):
        client = _client(lambda r: httpx.Response(200, json=OK_BODY))
        assert client._client.trust_env is False  # 防 7890 残留代理误判

    def test_key_configured(self):
        assert _client(lambda r: httpx.Response(200), key="sk-x").key_configured()
        assert not _client(lambda r: httpx.Response(200), key="").key_configured()


class TestErrorClassification:
    @pytest.mark.asyncio
    async def test_401_auth_error_no_retry(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"error": "invalid key"})

        client = _client(handler)
        with pytest.raises(DeepSeekAuthError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert calls["n"] == 1  # 不重试

    @pytest.mark.asyncio
    async def test_403_auth_error_no_retry(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(403, json={"error": "forbidden"})

        client = _client(handler)
        with pytest.raises(DeepSeekAuthError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_429_retries_once_then_ok(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        text = await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert text == '{"subtasks": []}'
        assert calls["n"] == 2  # 首次 429 + 重试 1 次

    @pytest.mark.asyncio
    async def test_500_retries_once_then_ok(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_500_exhausted_raises_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "overloaded"})

        client = _client(handler)
        with pytest.raises(DeepSeekHttpError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)

    @pytest.mark.asyncio
    async def test_network_error_retries_twice_then_ok(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert calls["n"] == 3  # 首次失败 + 重试 2 次

    @pytest.mark.asyncio
    async def test_timeout_retries_then_ok(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_network_exhausted_raises_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        client = _client(handler)
        with pytest.raises(DeepSeekNetworkError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)

    @pytest.mark.asyncio
    async def test_non_json_response_protocol_error_no_retry(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=b"<html>not json</html>")

        client = _client(handler)
        with pytest.raises(DeepSeekProtocolError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert calls["n"] == 1  # 协议错不重试

    @pytest.mark.asyncio
    async def test_missing_usage_protocol_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        client = _client(handler)
        with pytest.raises(DeepSeekProtocolError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_opens_after_3_consecutive_failures(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        client = _client(handler)
        for _ in range(3):
            with pytest.raises(DeepSeekAuthError):
                await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert client.circuit_open() is True

    @pytest.mark.asyncio
    async def test_circuit_resets_on_success(self):
        calls: dict = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(401, json={"error": "bad key"})
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        for _ in range(2):
            with pytest.raises(DeepSeekAuthError):
                await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert client.circuit_open() is False  # 2 次失败未达阈值
        await client.chat([{"role": "user", "content": "x"}], max_tokens=8)
        assert client._fail_count == 0  # 成功归零

    @pytest.mark.asyncio
    async def test_circuit_open_short_circuits_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=OK_BODY)

        client = _client(handler)
        client._fail_count = 3
        client._circuit_until = 9999999999.0
        with pytest.raises(DeepSeekNetworkError):
            await client.chat([{"role": "user", "content": "x"}], max_tokens=8)


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_false_when_no_key(self):
        client = _client(lambda r: httpx.Response(200, json=OK_BODY), key="")
        assert await client.health() is False

    @pytest.mark.asyncio
    async def test_health_true_on_ok(self):
        client = _client(lambda r: httpx.Response(200, json=OK_BODY), key="sk-x")
        assert await client.health() is True

    @pytest.mark.asyncio
    async def test_health_false_on_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        client = _client(handler, key="sk-x")
        assert await client.health() is False


class TestInputTrimming:
    def test_long_summary_truncated(self):
        from app.brain.sanitizer import truncate_head_tail

        long = "字" * 2000
        out = truncate_head_tail(long, 1200)
        assert len(out) <= 1200  # 总长不超上限（含省略标记）
        assert len(out) < len(long)
        assert "中间省略" in out
