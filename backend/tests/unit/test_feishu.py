"""FeishuProvider 单元测试：成功 / 业务错误 / 网络错误 / 消息体格式 / 限频

mock httpx：用 FakeClient 替换 _client（与 test_push_manager 同模式，不引第三方 mock）。
"""
from __future__ import annotations

import time

import httpx

from app.push.feishu import FeishuProvider

HOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"


class FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None, raw: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError(self._raw)
        return self._payload


class FakeClient:
    """记录最后一次请求体，返回预设响应；可配置抛网络异常"""

    def __init__(self, resp: FakeResp | None = None, exc: Exception | None = None):
        self.resp = resp
        self.exc = exc
        self.last_json: dict | None = None

    def post(self, url: str, json: dict | None = None, **kw):
        self.last_json = json
        if self.exc is not None:
            raise self.exc
        return self.resp


def make_provider(url: str = HOOK_URL, rl: int = 100) -> FeishuProvider:
    return FeishuProvider(url, rate_limit_per_minute=rl)


class TestSuccess:
    def test_text_success(self):
        """纯文本成功：code=0 → ok；消息体 msg_type=text"""
        p = make_provider()
        p._client = FakeClient(FakeResp(200, {"code": 0, "msg": "success"}))
        r = p.push("hello")
        assert r.ok and r.provider == "feishu"
        assert r.error is None
        body = p._client.last_json
        assert body["msg_type"] == "text"
        assert body["content"]["text"] == "hello"

    def test_title_uses_post_rich_text(self):
        """带标题 → 富文本 post：标题置顶 + 正文 text 段"""
        p = make_provider()
        p._client = FakeClient(FakeResp(200, {"code": 0, "msg": "success"}))
        r = p.push("正文内容", title="贾克斯 · codex")
        assert r.ok
        body = p._client.last_json
        assert body["msg_type"] == "post"
        zh = body["content"]["post"]["zh_cn"]
        assert zh["title"] == "贾克斯 · codex"
        assert zh["content"][0][0] == {"tag": "text", "text": "正文内容"}


class TestErrors:
    def test_business_error_not_retryable(self):
        """code != 0（webhook 失效等业务错误）→ 失败且不重试"""
        p = make_provider()
        p._client = FakeClient(FakeResp(200, {"code": 19001, "msg": "invalid token"}))
        r = p.push("x")
        assert not r.ok
        assert r.retryable is False
        assert "19001" in (r.error or "")

    def test_network_error_retryable(self):
        """httpx 网络异常 → retryable=True（供 manager 重试）"""
        p = make_provider()
        p._client = FakeClient(exc=httpx.ConnectError("conn refused"))
        r = p.push("x")
        assert not r.ok
        assert r.retryable is True

    def test_5xx_retryable(self):
        """5xx 服务端瞬时错误 → retryable=True"""
        p = make_provider()
        p._client = FakeClient(FakeResp(502, {"code": 1}))
        r = p.push("x")
        assert not r.ok
        assert r.retryable is True

    def test_4xx_not_retryable(self):
        """4xx（URL 非法）→ 不重试"""
        p = make_provider()
        p._client = FakeClient(FakeResp(404, {}))
        r = p.push("x")
        assert not r.ok
        assert r.retryable is False

    def test_non_json_response_no_escape(self):
        """响应非 JSON → 失败且不抛异常（不逃逸）"""
        p = make_provider()
        p._client = FakeClient(FakeResp(200, raw="<html>gateway</html>"))
        r = p.push("x")
        assert not r.ok
        assert r.retryable is False
        assert "非 JSON" in (r.error or "")


class TestRateLimit:
    def test_quota_only_on_success(self):
        """限频：仅成功后占配额，失败不占"""
        p = make_provider(rl=2)

        p._client = FakeClient(FakeResp(200, {"code": 0}))
        assert p.push("a").ok
        assert len(p._timestamps) == 1, "成功后占 1 配额"

        p._client = FakeClient(FakeResp(200, {"code": 19001, "msg": "err"}))
        assert not p.push("b").ok
        assert len(p._timestamps) == 1, "失败不占配额"

        p._client = FakeClient(FakeResp(200, {"code": 0}))
        assert p.push("c").ok
        assert len(p._timestamps) == 2, "2 次成功占满配额"
        assert p._allow() is False, "配额已满应拒绝"

    def test_rate_limited_returns_failure(self):
        """配额满 → 直接返回失败（不发起 HTTP）"""
        p = make_provider(rl=1)
        p._timestamps = [time.time()]  # 手动填满配额（当前窗口内）
        calls = 0

        class CountingClient(FakeClient):
            def post(self, url, json=None, **kw):
                nonlocal calls
                calls += 1
                return FakeResp(200, {"code": 0})

        p._client = CountingClient()
        r = p.push("x")
        assert not r.ok
        assert "限频" in (r.error or "")
        assert calls == 0, "限频时不发 HTTP"


class TestInit:
    def test_missing_webhook_raises(self):
        """未配置 webhook → ValueError（manager 捕获后跳过）"""
        try:
            FeishuProvider("")
        except ValueError as e:
            assert "FEISHU_WEBHOOK_URL" in str(e)
        else:  # pragma: no cover
            raise AssertionError("应抛 ValueError")


class TestManagerIntegration:
    def test_manager_registers_feishu_when_webhook_set(self):
        """PushManager 集成：配置了 webhook → feishu provider 注册"""
        from app.config import FeishuConfig, NtfyConfig, PushConfig, Settings, WecomConfig
        from app.push.manager import PushManager

        cfg = PushConfig(
            providers=["feishu", "ntfy"],
            wecom=WecomConfig(enabled=False),
            feishu=FeishuConfig(enabled=True),
            ntfy=NtfyConfig(enabled=False),
        )
        mgr = PushManager(cfg, Settings(feishu_webhook_url=HOOK_URL))
        assert "feishu" in mgr.available()

    def test_manager_skips_feishu_without_webhook(self):
        """无 webhook → feishu 跳过（不注册，不抛异常）"""
        from app.config import FeishuConfig, NtfyConfig, PushConfig, Settings, WecomConfig
        from app.push.manager import PushManager

        cfg = PushConfig(
            providers=["feishu"],
            wecom=WecomConfig(enabled=False),
            feishu=FeishuConfig(enabled=True),
            ntfy=NtfyConfig(enabled=False),
        )
        mgr = PushManager(cfg, Settings(feishu_webhook_url=""))
        assert "feishu" not in mgr.available()

    def test_manager_feishu_routes_after_wecom_fail(self):
        """路由：feishu 首个 provider，成功即返回（manager 语义）"""
        from app.config import FeishuConfig, NtfyConfig, PushConfig, Settings, WecomConfig
        from app.push.manager import PushManager

        cfg = PushConfig(
            providers=["feishu", "ntfy"],
            wecom=WecomConfig(enabled=False),
            feishu=FeishuConfig(enabled=True),
            ntfy=NtfyConfig(enabled=False),
        )
        mgr = PushManager(cfg, Settings(feishu_webhook_url=HOOK_URL))
        p = mgr._providers["feishu"]
        p._client = FakeClient(FakeResp(200, {"code": 0, "msg": "success"}))
        r = mgr.push("manager 集成测试")
        assert r.ok and r.provider == "feishu"
