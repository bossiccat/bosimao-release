"""cloudapi 入口 hello proof 签发装配契约。

背景（真实缺陷）：`cloudapi/main.py` 构造 `create_secured_voice_router(...)` 时
**没有传 `hello_service`**，并带注释「hello 不上云」。后果是
`routes_voice_security_context.py` 里 `sidecar_sign = SidecarSignService(...) if
hello_service is not None else None` 恒为 None，于是
`routes_voice_sessions.py` 的 `if deps.sidecar_sign is None: return 50303` 恒成立——
`POST /api/v1/voice/session/sign` 永远 50303，sidecar 永远进不了房间，整条云端语音
链路是死的。

这与已批准设计矛盾：docs/plans/2026-09-07-voice-cloud-session-migration.md §2 把
`session / pending claim / hello proof` 列为 jax-voice-api 职责。本文件把「装配确实
接上了」钉进 CI：一条静态 AST 断言，一条真起 app 发请求的行为断言。

不测任何密钥值：行为用例里的 hello 配置全是显式假值（FAKE-*）。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
CLOUDAPI_MAIN = ROOT / "cloudapi" / "main.py"
SECURED_ROUTES = BACKEND / "app" / "api" / "routes_voice_secured.py"

HELLO_REDEEM_PATH = "/api/v1/voice/internal/rtc-bridge/hello-redeem"

# 兑付行为用例用的**显式假值**，不是任何真实凭据/密钥。
FAKE_TRUSTED_GATEWAY_SOURCE = "10.15.254.10"
FAKE_UNTRUSTED_GATEWAY_SOURCE = "203.0.113.9"  # TEST-NET-3 文档保留段，非真实来源

# 开发模式行为用例用的**显式假值**，不是任何真实凭据/密钥。
FAKE_OWNER_CREDENTIAL = "fake-contract-owner-credential"
FAKE_SIDECAR_CREDENTIAL = "fake-contract-sidecar-credential"
FAKE_HELLO_PRIVATE_KEY_PEM = "FAKE-CONTRACT-hello-private-key-pem-not-a-real-key"
FAKE_HELLO_PUBLIC_KEY_PEM = "FAKE-CONTRACT-hello-public-key-pem-not-a-real-key"
FAKE_RTC_BRIDGE_CREDENTIAL = "fake-contract-rtc-bridge-credential"
FAKE_RTC_BRIDGE_CERT_BINDING = "fake-contract-cert-binding"
FAKE_GATEWAY_SHARED_ASSERTION = "fake-contract-gateway-assertion"
FAKE_TRTC_SDKAPPID = "1400000001"
FAKE_TRTC_SECRETKEY = "fake-contract-trtc-secret-key"


def _find_call(source: str, func_name: str) -> ast.Call:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == func_name:
                return node
    raise AssertionError(f"未找到 {func_name}(...) 调用")


def _keyword_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def test_cloudapi_passes_hello_service_to_secured_router() -> None:
    """静态装配断言：cloudapi 必须把 hello_service 以非 None 传入。

    这条会在「又有人把 hello_service 显式置空/删掉」时立刻变红——那正是导致
    /session/sign 恒 50303 的写法。
    """
    source = CLOUDAPI_MAIN.read_text(encoding="utf-8")
    call = _find_call(source, "create_secured_voice_router")
    kwargs = _keyword_names(call)

    assert "hello_service" in kwargs, "create_secured_voice_router 未传 hello_service"
    hello_kw = next(kw for kw in call.keywords if kw.arg == "hello_service")
    assert not (
        isinstance(hello_kw.value, ast.Constant) and hello_kw.value.value is None
    ), "hello_service 被显式置为 None —— 会让 sidecar_sign 恒为 None（50303）"

    assert "hello_certificate_binding" in kwargs
    assert "hello_gateway_assertion_hash" in kwargs


def test_cloudapi_no_longer_claims_hello_is_not_cloud_hosted() -> None:
    """「hello 不上云」的过时注释与已批准设计相矛盾，必须已被改写。"""
    source = CLOUDAPI_MAIN.read_text(encoding="utf-8")
    assert "hello 不上云" not in source
    assert "hello_service=None：hello" not in source
    # 正向标记：文档说明 hello proof 签发是云端控制面职责。
    assert "hello proof" in source and "50303" in source


def test_secured_router_forwards_gateway_assertion_hash() -> None:
    """参数不得在装配层被吞掉：build_hello_router 必须收到 gateway_assertion_hash。"""
    source = SECURED_ROUTES.read_text(encoding="utf-8")
    call = _find_call(source, "build_hello_router")
    kwargs = _keyword_names(call)
    assert "expected_certificate_binding" in kwargs
    assert "gateway_assertion_hash" in kwargs


def _load_entrypoint(monkeypatch: pytest.MonkeyPatch):
    """开发模式加载 cloudapi/main.py，并注入**全套 hello 假配置**后返回模块。

    与 test_cloudrun_entrypoint_serves_http.py 同一加载方式：开发模式（不设
    VOICE_PRODUCTION）走 SQLite 夹具，无需 psycopg / 真实数据库。
    """
    monkeypatch.delenv("VOICE_PRODUCTION", raising=False)
    monkeypatch.setenv("VOICE_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("VOICE_OWNER_CREDENTIAL", FAKE_OWNER_CREDENTIAL)
    monkeypatch.setenv("VOICE_SIDECAR_CREDENTIAL", FAKE_SIDECAR_CREDENTIAL)
    monkeypatch.setenv("TRTC_SDKAPPID", FAKE_TRTC_SDKAPPID)
    monkeypatch.setenv("TRTC_SECRETKEY", FAKE_TRTC_SECRETKEY)
    monkeypatch.setenv("VOICE_HELLO_PRIVATE_KEY_PEM", FAKE_HELLO_PRIVATE_KEY_PEM)
    monkeypatch.setenv("VOICE_HELLO_PUBLIC_KEY_PEM", FAKE_HELLO_PUBLIC_KEY_PEM)
    monkeypatch.setenv("VOICE_RTC_BRIDGE_CREDENTIAL", FAKE_RTC_BRIDGE_CREDENTIAL)
    monkeypatch.setenv("VOICE_RTC_BRIDGE_CERT_BINDING", FAKE_RTC_BRIDGE_CERT_BINDING)
    monkeypatch.setenv("VOICE_GATEWAY_SHARED_ASSERTION", FAKE_GATEWAY_SHARED_ASSERTION)

    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    spec = importlib.util.spec_from_file_location(
        "jax_voice_api_hello_wiring_entrypoint", CLOUDAPI_MAIN
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cloudapi_mounts_hello_redeem_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为断言：真起 app，确认 hello-redeem 路由已挂载。

    build_hello_router 只在 hello_service is not None 时被 include；路由存在即证明
    hello 运行时确实装配成功（未被降级为 None）。
    """
    module = _load_entrypoint(monkeypatch)
    # 用 OpenAPI 公开契约取路径集：新版 FastAPI 把 include_router 惰性包成
    # _IncludedRouter，app.routes 顶层不再平铺子路由，openapi() 才是稳定视图。
    paths = set(module.app.openapi()["paths"])
    assert HELLO_REDEEM_PATH in paths, sorted(paths)


def test_session_sign_no_longer_returns_50303(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为断言：真发 /session/sign，证明 `deps.sidecar_sign is not None`。

    走完整鉴权链（无 mock、无旁路）：owner/sidecar/TRTC 配置 + 合法 sidecar bearer +
    新鲜 nonce + 开启 cloud_processing。因为没有 pending claim，期望落到
    SignClaimRejected → 40901（state_conflict）。若 hello 未装配，会在到达 sign 之前
    命中 `if deps.sidecar_sign is None: return 50303` —— 这正是本用例要拦截的回归。
    """
    module = _load_entrypoint(monkeypatch)
    # 真实 privacy 配置（非 mock）：默认即为 True，显式设一遍以消除共享 SQLite 的串扰。
    module.store.set_setting("privacy:cloud_processing_enabled", "true")

    client = TestClient(module.app)
    response = client.post(
        "/api/v1/voice/session/sign",
        headers={
            "Authorization": f"Bearer {FAKE_SIDECAR_CREDENTIAL}",
            "X-Request-Nonce": uuid.uuid4().hex,
        },
        json={
            "session_id": "contract-session",
            "claim_token": "c" * 32,
            "device_id": "contract-device",
            "user_id": "jax-pc-sidecar",
        },
    )
    payload = response.json()
    assert payload["code"] != 50303, payload
    # 没有 pending claim → 状态冲突；重点不是这个码，而是它已越过 sidecar_sign 判空。
    assert payload["code"] == 40901, payload
    assert response.status_code == 409, response.text


# ---------------------------------------------------------------------------
# 兑付链路服务端装配（hello-redeem）
#
# 背景（真实缺陷）：cloudapi 入口已签发 hello proof，但兑付端缺少两处服务端装配，
# 使 POST /api/v1/voice/internal/rtc-bridge/hello-redeem 恒 40114：
#   1. 未挂载受信网关身份中间件 → trusted_certificate_binding(scope) 拿不到盖章值；
#   2. CredentialValidator 未传 rtc_bridge_credential_hash → verify_rtc_bridge 恒 40101。
# 下面一条静态 AST 断言 + 两条行为断言把这个缺口钉进 CI。
# ---------------------------------------------------------------------------


def test_cloudapi_passes_rtc_bridge_credential_hash_to_validator() -> None:
    """静态装配断言：CredentialValidator(...) 必须传 rtc_bridge_credential_hash。

    缺此参数时 backend/app/voice/auth.py:161-166 的 verify_rtc_bridge 恒 40101，
    兑付 fail-closed 终局。
    """
    source = CLOUDAPI_MAIN.read_text(encoding="utf-8")
    call = _find_call(source, "CredentialValidator")
    kwargs = _keyword_names(call)
    assert "rtc_bridge_credential_hash" in kwargs, (
        "CredentialValidator 未传 rtc_bridge_credential_hash —— verify_rtc_bridge 恒 40101，"
        "hello-redeem 兑付必失败"
    )


def test_cloudapi_mounts_trusted_gateway_identity_middleware() -> None:
    """静态装配断言：cloudapi 必须挂载受信网关身份中间件。

    断言存在 app.add_middleware(TrustedGatewayIdentityMiddleware, ...)，且三个必要参数
    （allowed_hosts / gateway_assertion_hash / certificate_binding）都在场——缺任一都会
    让 trusted_certificate_binding(scope) 永远返回 None → 兑付恒 40114。
    """
    source = CLOUDAPI_MAIN.read_text(encoding="utf-8")
    assert "TrustedGatewayIdentityMiddleware" in source, "中间件未导入/未引用"

    mounted = False
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "add_middleware" or not node.args:
            continue
        first = node.args[0]
        first_name = (
            first.id if isinstance(first, ast.Name) else getattr(first, "attr", None)
        )
        if first_name != "TrustedGatewayIdentityMiddleware":
            continue
        kwargs = _keyword_names(node)
        assert "allowed_hosts" in kwargs, "中间件缺少 allowed_hosts（受信来源白名单）"
        assert "gateway_assertion_hash" in kwargs, "中间件缺少 gateway_assertion_hash"
        assert "certificate_binding" in kwargs, "中间件缺少 certificate_binding"
        mounted = True

    assert mounted, "cloudapi 未挂载 TrustedGatewayIdentityMiddleware"


def _redeem_request(client: TestClient) -> dict:
    """向兑付端发一次请求（带正确断言头），返回响应 JSON。

    来源 IP 由 `client=` 参数控制：来源不受信时即使断言正确也必须被拒，
    以此证明「来源 + 断言」双条件门禁没被绕过。
    """
    response = client.post(
        HELLO_REDEEM_PATH,
        headers={
            "Authorization": f"Bearer {FAKE_RTC_BRIDGE_CREDENTIAL}",
            "X-Internal-Gateway-Assertion": FAKE_GATEWAY_SHARED_ASSERTION,
        },
        json={},
    )
    return response.json()


def test_hello_redeem_rejects_untrusted_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为断言：来源不在白名单 → 必须 40114（门禁确实生效）。

    甚至带上正确断言头也不行——来源白名单是独立的第一个条件。
    """
    module = _load_entrypoint(monkeypatch)
    client = TestClient(
        module.app, client=(FAKE_UNTRUSTED_GATEWAY_SOURCE, 40001)
    )
    payload = _redeem_request(client)
    assert payload["code"] == 40114, payload


def test_hello_redeem_passes_identity_gate_for_trusted_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行为断言：来源在白名单内且断言正确 → 不再 40114（身份门禁通过）。

    把来源 IP 加进 VOICE_TRUSTED_GATEWAY_HOSTS，用 TestClient(client=(ip, port)) 伪造
    来源。身份门禁通过后落到 body 校验，`{}` 不是合法 hello → 40021；重点是码不是 40114。
    """
    monkeypatch.setenv("VOICE_TRUSTED_GATEWAY_HOSTS", FAKE_TRUSTED_GATEWAY_SOURCE)
    module = _load_entrypoint(monkeypatch)
    client = TestClient(module.app, client=(FAKE_TRUSTED_GATEWAY_SOURCE, 40002))
    payload = _redeem_request(client)
    assert payload["code"] != 40114, payload
    # 身份门禁已过 → 落在 HelloRedeemRequest 校验失败（40021），非身份拒绝。
    assert payload["code"] == 40021, payload

