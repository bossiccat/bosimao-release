"""FeishuCallbackService + 回调端点测试：challenge 验证 / 消息解析 / EventBus 广播

覆盖：URL 验证回显、token 验签、text/audio 消息解析、非消息事件忽略、
      端点 POST /api/v1/push/feishu/callback 的 200 ack 与 challenge 响应。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_feishu import create_feishu_router
from app.config import FeishuConfig
from app.core.events import EVT_FEISHU_MSG, EventBus
from app.push.feishu_events import FeishuCallbackError, FeishuCallbackService


class CollectBus(EventBus):
    """记录广播的测试总线"""

    def __init__(self):
        super().__init__()
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, data: dict) -> None:
        self.emitted.append((event_type, data))


def make_service(bus: CollectBus | None = None, token: str = "") -> FeishuCallbackService:
    return FeishuCallbackService(bus or CollectBus(), verification_token=token)


def message_payload(
    msg_type: str = "text",
    content: str = '{"text":"你好"}',
    event_type: str = "im.message.receive_v1",
    token: str = "evt-token",
) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": "evt-1", "event_type": event_type, "token": token},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_123"}, "sender_type": "user"},
            "message": {
                "message_id": "om_456",
                "chat_id": "oc_789",
                "chat_type": "p2p",
                "message_type": msg_type,
                "content": content,
            },
        },
    }


class TestChallenge:
    @pytest.mark.asyncio
    async def test_challenge_echo(self):
        """URL 验证：challenge 原样回显"""
        svc = make_service()
        resp = await svc.handle({"type": "url_verification", "challenge": "ajls384kdjx98XX"})
        assert resp == {"challenge": "ajls384kdjx98XX"}

    @pytest.mark.asyncio
    async def test_challenge_missing_raises(self):
        svc = make_service()
        with pytest.raises(FeishuCallbackError) as ei:
            await svc.handle({"type": "url_verification"})
        assert ei.value.status_code == 400
        assert "challenge" in str(ei.value)

    @pytest.mark.asyncio
    async def test_challenge_token_mismatch_raises(self):
        """配置了 verification_token → 不匹配须 403 拒绝"""
        svc = make_service(token="expect-token")
        with pytest.raises(FeishuCallbackError) as ei:
            await svc.handle(
                {"type": "url_verification", "challenge": "c", "token": "wrong"}
            )
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_challenge_token_match_ok(self):
        svc = make_service(token="expect-token")
        resp = await svc.handle(
            {"type": "url_verification", "challenge": "c", "token": "expect-token"}
        )
        assert resp == {"challenge": "c"}


class TestMessage:
    @pytest.mark.asyncio
    async def test_text_message_emitted(self):
        """text 消息 → 广播 EVT_FEISHU_MSG（含文本与发送者）"""
        bus = CollectBus()
        svc = make_service(bus)
        resp = await svc.handle(message_payload())
        assert resp == {}, "事件须立即 ack（飞书 3s 超时）"
        assert len(bus.emitted) == 1
        evt, data = bus.emitted[0]
        assert evt == EVT_FEISHU_MSG
        assert data["message_type"] == "text"
        assert data["text"] == "你好"
        assert data["message_id"] == "om_456"
        assert data["chat_id"] == "oc_789"
        assert data["file_key"] == ""

    @pytest.mark.asyncio
    async def test_audio_message_file_key(self):
        """audio 消息 → 解析 file_key（语音对话链路占位，O-014）"""
        bus = CollectBus()
        svc = make_service(bus)
        await svc.handle(
            message_payload(msg_type="audio", content='{"file_key":"file_audio_01"}')
        )
        assert len(bus.emitted) == 1
        _, data = bus.emitted[0]
        assert data["message_type"] == "audio"
        assert data["file_key"] == "file_audio_01"
        assert data["text"] == ""

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self):
        """非 im.message.receive_v1 事件 → 不广播"""
        bus = CollectBus()
        svc = make_service(bus)
        await svc.handle(message_payload(event_type="im.chat.member.bot.added_v1"))
        assert bus.emitted == []

    @pytest.mark.asyncio
    async def test_unsupported_message_type_ignored(self):
        """非 text/audio 消息（如图片）→ 不广播"""
        bus = CollectBus()
        svc = make_service(bus)
        await svc.handle(message_payload(msg_type="image", content='{"image_key":"img"}'))
        assert bus.emitted == []

    @pytest.mark.asyncio
    async def test_message_token_mismatch_raises(self):
        svc = make_service(token="expect-token")
        with pytest.raises(FeishuCallbackError) as ei:
            await svc.handle(message_payload(token="wrong"))
        assert ei.value.status_code == 403

    @pytest.mark.asyncio
    async def test_malformed_content_not_crash(self):
        """content 非 JSON（脏数据）→ 不崩溃，text 原样返回"""
        bus = CollectBus()
        svc = make_service(bus)
        await svc.handle(message_payload(content="not-json"))
        assert len(bus.emitted) == 1
        assert bus.emitted[0][1]["text"] == "not-json"


class TestEndpoint:
    def _client(self, bus: CollectBus) -> TestClient:
        app = FastAPI()
        app.include_router(create_feishu_router(bus, FeishuConfig()))
        return TestClient(app)

    def test_challenge_endpoint(self):
        """POST /api/v1/push/feishu/callback：URL 验证回显 challenge"""
        client = self._client(CollectBus())
        resp = client.post(
            "/api/v1/push/feishu/callback",
            json={"type": "url_verification", "challenge": "abc123"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "abc123"}

    def test_message_endpoint_ack_and_broadcast(self):
        """消息事件端点：200 ack + 广播入总线"""
        bus = CollectBus()
        client = self._client(bus)
        resp = client.post(
            "/api/v1/push/feishu/callback",
            json=message_payload(),
        )
        assert resp.status_code == 200
        assert resp.json() == {}
        assert len(bus.emitted) == 1
        assert bus.emitted[0][0] == EVT_FEISHU_MSG

    def test_invalid_challenge_returns_400(self):
        client = self._client(CollectBus())
        resp = client.post("/api/v1/push/feishu/callback", json={"type": "url_verification"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == 40001
