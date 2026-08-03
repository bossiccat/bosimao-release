"""PushManager 单元测试：路由顺序 / 熔断切换 / 半开恢复 / 限频计数 / 非 JSON 不逃逸"""
from __future__ import annotations

import time

import pytest

from app.config import CircuitBreakerConfig, NtfyConfig, PushConfig, Settings, WecomConfig
from app.push.base import PushResult
from app.push.manager import PushManager
from app.push.wecom import WecomProvider


class FakeProvider:
    """按预设队列返回结果；队列空则成功"""

    def __init__(self, name: str, results: list[PushResult] | None = None):
        self.name = name
        self.results = list(results or [])
        self.calls = 0

    def push(self, text, image=None, title=None):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return PushResult(True, self.name)


class BoomProvider:
    """push 直接抛异常（模拟未预期错误 / 逃逸场景）"""

    name = "boom"

    def __init__(self):
        self.calls = 0

    def push(self, text, image=None, title=None):
        self.calls += 1
        raise RuntimeError("boom")


def make_cfg(**kw) -> PushConfig:
    defaults = dict(
        providers=["a", "b"],
        wecom=WecomConfig(enabled=False),
        ntfy=NtfyConfig(enabled=False),
        circuit_breaker=CircuitBreakerConfig(fail_threshold=3, cooldown_seconds=300),
    )
    defaults.update(kw)
    return PushConfig(**defaults)


def make_mgr(cfg: PushConfig) -> PushManager:
    return PushManager(cfg, Settings())


class TestRouting:
    def test_first_success_wins(self):
        """路由顺序：第一个成功即返回，后续 provider 不被调用"""
        a, b = FakeProvider("a"), FakeProvider("b")
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": a, "b": b}
        r = mgr.push("x")
        assert r.ok and r.provider == "a"
        assert a.calls == 1 and b.calls == 0

    def test_failover_to_next(self):
        """a 失败（业务错误）→ 路由到 b 成功"""
        a = FakeProvider("a", [PushResult(False, "a", "err", retryable=False)])
        b = FakeProvider("b")
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": a, "b": b}
        r = mgr.push("x")
        assert r.ok and r.provider == "b"
        assert a.calls == 1 and b.calls == 1


class TestCircuitBreaker:
    def test_open_after_threshold(self):
        """连续 fail_threshold 次失败 → 熔断；熔断期 provider 不再被调用"""
        fail = FakeProvider("a", [PushResult(False, "a", "e")] * 10)
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": fail}
        for _ in range(3):
            mgr.push("x")
        assert mgr._circuit_open("a"), "3 次失败后应熔断"
        assert fail.calls == 3
        before = fail.calls
        mgr.push("x")
        assert fail.calls == before, "熔断期不应调用 provider"
        assert not mgr.push("x").ok

    def test_half_open_probe_success_closes(self):
        """冷却到期 → 半开 probe 成功 → fail_count 归零关闭熔断"""
        fail = FakeProvider(
            "a",
            [
                PushResult(False, "a", "e"),
                PushResult(False, "a", "e"),
                PushResult(False, "a", "e"),
                PushResult(True, "a"),  # probe 成功
                PushResult(True, "a"),
            ],
        )
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": fail}
        for _ in range(3):
            mgr.push("x")
        assert mgr._circuit_open("a")
        mgr._cooldown_until["a"] = time.time() - 1  # 冷却到期
        r = mgr.push("x")
        assert r.ok, "半开 probe 应成功"
        assert mgr._fail_count.get("a", 0) == 0, "成功后 fail_count 归零"
        assert not mgr._circuit_open("a"), "熔断应关闭"
        assert not mgr._in_half_open("a")
        # 后续请求正常放行
        r2 = mgr.push("x")
        assert r2.ok

    def test_half_open_probe_fail_reopens(self):
        """冷却到期 → probe 失败 → 立即重新熔断"""
        fail = FakeProvider("a", [PushResult(False, "a", "e")] * 10)
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": fail}
        for _ in range(3):
            mgr.push("x")
        mgr._cooldown_until["a"] = time.time() - 1
        r = mgr.push("x")  # probe 失败
        assert not r.ok
        assert mgr._circuit_open("a"), "probe 失败应重新熔断"
        before = fail.calls
        mgr.push("x")
        assert fail.calls == before, "重新熔断后不应放行"


class TestRetry:
    def test_network_error_retried_once(self):
        """网络类错误（retryable=True）重试 1 次，第二次成功"""
        a = FakeProvider(
            "a",
            [
                PushResult(False, "a", "timeout", retryable=True),
                PushResult(True, "a"),
            ],
        )
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": a}
        r = mgr.push("x")
        assert r.ok and r.provider == "a"
        assert a.calls == 2, "网络错误应重试 1 次（共调用 2 次）"

    def test_business_error_not_retried(self):
        """业务错误（retryable=False）不重试，只调用 1 次"""
        a = FakeProvider("a", [PushResult(False, "a", "errcode", retryable=False)])
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": a}
        r = mgr.push("x")
        assert not r.ok
        assert a.calls == 1, "业务错误不应重试"

    def test_exception_does_not_escape(self):
        """provider 抛异常 → 管理器捕获并记为失败，不向外抛"""
        boom = BoomProvider()
        mgr = make_mgr(make_cfg())
        mgr._providers = {"a": boom}
        r = mgr.push("x")  # 不应抛异常
        assert not r.ok
        assert boom.calls == 1


class TestWecomRateLimit:
    def test_quota_only_on_success(self):
        """wecom 限频：仅成功后占配额，失败不占"""
        w = WecomProvider("https://dummy.example/hook", rate_limit_per_minute=3)

        class FakeResp:
            def __init__(self, ok: bool):
                self.status_code = 200 if ok else 200
                self._ok = ok

            def json(self):
                return {"errcode": 0} if self._ok else {"errcode": 93000}

        class FakeClient:
            def __init__(self, resp):
                self.resp = resp

            def post(self, *a, **k):
                return self.resp

        w._client = FakeClient(FakeResp(ok=True))
        assert w.push("x").ok
        assert len(w._timestamps) == 1, "成功后占 1 配额"
        w._client = FakeClient(FakeResp(ok=False))
        assert not w.push("x").ok
        assert len(w._timestamps) == 1, "失败不占配额"
        w._client = FakeClient(FakeResp(ok=True))
        w.push("x")
        w.push("x")
        assert len(w._timestamps) == 3, "3 次成功占满配额"
        assert w._allow() is False, "配额已满应拒绝"

    def test_non_json_response_no_escape(self):
        """wecom 响应非 JSON → 返回失败且不抛异常"""
        w = WecomProvider("https://dummy.example/hook", rate_limit_per_minute=3)

        class FakeResp:
            status_code = 502

            def json(self):
                raise ValueError("非 JSON 响应")

        class FakeClient:
            def post(self, *a, **k):
                return FakeResp()

        w._client = FakeClient()
        r = w.push("x")
        assert not r.ok
        assert r.retryable is False
        assert "非 JSON" in (r.error or "")
