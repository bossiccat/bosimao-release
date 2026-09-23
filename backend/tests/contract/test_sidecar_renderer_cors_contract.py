"""RP-07 后续修复：sidecar renderer（file:// 页面）→ backend 控制面 fetch 的 CORS 契约。

背景（2026-09-02 定位）：rtc.js 意图轮询用 Chromium fetch（"Failed to fetch" 为
Chromium 报错文案），从 file:// 页面（Origin: null）携带 Authorization +
X-Request-Nonce 自定义头发起请求 → 触发 CORS preflight（OPTIONS）→ backend 无
CORSMiddleware → 405 → 轮询永远失败。TLS 层已由 main.js certificate-error 钉
CA 指纹解决，本契约锁定 HTTP 层 CORS 行为。

安全边界（fail-closed）：
- 仅放行 file:// renderer 的 Origin "null"（sidecar 唯一合法调用方形态）；
- 普通网页 Origin 不得获得放行头；
- allow_credentials 必须为 False（控制面用 Bearer，不用 cookie）；
- preflight 本身不执行业务（OPTIONS 由中间件应答，不进 guard）——
  凭证缺失的实请求仍走既有 401 门，CORS 不改变鉴权语义。
"""
import sys
from pathlib import Path

sys.path.insert(0, "backend")

# 环境前提由 backend/tests/conftest.py 统一提供（hello 的 5 项安全能力）。
# 本文件是 contract/ 里唯一在模块级 import app.main 的：app.main 在**导入期**装配
# 生产应用，而 hello 装配 fail-closed（backend/app/voice/hello_runtime.py:42-45），
# 干净检出上缺任一项会让**整层契约**在收集阶段就死掉（2026-09-24 CI 实测）。
# 别在这里再抄一份环境设置——重复的声明迟早会漂移。
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PENDING_PATH = "/api/v1/voice/session/pending"
REQUEST_HEADERS = "authorization, x-request-nonce, content-type"


def _client() -> TestClient:
    # 不进入 lifespan（TestClient 不用 with），仅测中间件行为
    return TestClient(app)


def test_preflight_from_null_origin_is_allowed() -> None:
    """If renderer preflight arrives from file:// (Origin: null), the app must answer 200 with CORS grant headers."""
    resp = _client().options(
        PENDING_PATH,
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": REQUEST_HEADERS,
        },
    )
    assert resp.status_code == 200, f"preflight 被拒: {resp.status_code} {resp.text[:200]}"
    assert resp.headers.get("access-control-allow-origin") == "null"
    allow_headers = resp.headers.get("access-control-allow-headers", "").lower()
    for required in ("authorization", "x-request-nonce", "content-type"):
        assert required in allow_headers, f"preflight 未放行 {required}: {allow_headers}"


def test_simple_get_from_null_origin_gets_grant_header() -> None:
    """When the renderer GETs the control plane from file://, the response must carry the CORS grant header."""
    resp = _client().get(PENDING_PATH, headers={"Origin": "null"})
    assert resp.headers.get("access-control-allow-origin") == "null"
    # 鉴权语义不被 CORS 改动：无凭证仍 401（守卫在前）
    assert resp.status_code == 401


def test_web_origin_is_not_granted() -> None:
    """If a regular web origin sends preflight, the app must not return a grant header (fail-closed)."""
    resp = _client().options(
        PENDING_PATH,
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": REQUEST_HEADERS,
        },
    )
    granted = resp.headers.get("access-control-allow-origin")
    assert granted != "https://evil.example", "普通网页 Origin 不应获得放行头"


def test_preflight_does_not_require_credentials() -> None:
    """Preflight is answered by middleware before guards: no Authorization needed, and it must not consume or leak anything."""
    resp = _client().options(
        PENDING_PATH,
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": REQUEST_HEADERS,
        },
    )
    assert resp.status_code == 200
    assert b"intents" not in resp.content.lower()
