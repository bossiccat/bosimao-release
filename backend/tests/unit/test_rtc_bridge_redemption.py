from __future__ import annotations

import json

import httpx
import pytest

from rtc_bridge.redemption import HelloRedemptionClient, HelloRedemptionError, validate_hello


def _hello() -> dict:
    return {
        "type": "hello", "proof": "proof-current", "nonce": "nonce-current-1234",
        "jti": "jti-current", "session_id": "session-current",
        "device_id": "device-current", "room_id": "room-current",
        "sidecar_user_id": "jax-pc-sidecar", "generation": 13,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }


def test_validate_hello_requires_exact_frozen_shape() -> None:
    assert validate_hello(_hello()) == _hello()
    for field in _hello():
        invalid = _hello()
        invalid.pop(field)
        with pytest.raises(HelloRedemptionError):
            validate_hello(invalid)
    extra = _hello()
    extra["sdk_version"] = "unfrozen"
    with pytest.raises(HelloRedemptionError):
        validate_hello(extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 409, 503])
async def test_non_200_is_rejected_without_retry(status: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"code": 40111}, request=request)

    client = HelloRedemptionClient(
        base_url="https://control-plane.internal",
        service_credential="service-secret",
        ca_file="ca.pem", client_cert_file="client.pem", client_key_file="client.key",
        gateway_assertion="gateway-secret", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HelloRedemptionError):
        await client.redeem(_hello())
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "bad_json", "mismatch"])
async def test_transport_json_and_binding_fail_closed(mode: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "bad_json":
            return httpx.Response(200, text="not-json", request=request)
        hello = _hello()
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "redeemed": True,
                "session_id": hello["session_id"], "device_id": hello["device_id"],
                "room_id": "wrong-room", "sidecar_user_id": hello["sidecar_user_id"],
                "generation": hello["generation"], "expires_at": "2026-08-25T10:00:00Z",
            },
            "message": "",
        }, request=request)

    client = HelloRedemptionClient(
        base_url="https://control-plane.internal", service_credential="service-secret",
        ca_file="ca.pem", client_cert_file="client.pem", client_key_file="client.key",
        gateway_assertion="gateway-secret", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HelloRedemptionError):
        await client.redeem(_hello())


@pytest.mark.asyncio
async def test_success_posts_once_with_service_and_gateway_assertion() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        hello = json.loads(request.content)
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "redeemed": True,
                **{key: hello[key] for key in (
                    "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
                )},
                "expires_at": "2026-08-25T10:00:00Z",
            },
            "message": "",
        }, request=request)

    client = HelloRedemptionClient(
        base_url="https://control-plane.internal", service_credential="service-secret",
        ca_file="ca.pem", client_cert_file="client.pem", client_key_file="client.key",
        gateway_assertion="gateway-secret", transport=httpx.MockTransport(handler),
    )
    result = await client.redeem(_hello())
    assert result["redeemed"] is True
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer service-secret"
    assert calls[0].headers["x-internal-gateway-assertion"] == "gateway-secret"


@pytest.mark.asyncio
async def test_rejection_message_carries_business_code_for_attribution() -> None:
    """非 200 必须把**业务错误码**写进异常消息，否则兑付失败无法归因。

    为什么单独立这一条：40101 / 40114 / 40021 各自精确对应一道不同的校验
    （服务凭证不被接受 / 断言哈希或证书绑定不一致 / 会话状态）。旧实现把这几个
    统一吞成 "redemption rejected"，实测为此白排查了多轮。

    这条埋点此前**没有任何测试守护**：2026-09-14 变异校验发现，把它改成恒 "n/a"
    时 contract 20 例 + unit 65 例**全绿** ⇒ 等于没有保护。现补上。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"code": 40114, "data": None,
                  "message": "rtc_bridge_service_auth_failed"},
            request=request,
        )

    client = HelloRedemptionClient(
        base_url="https://control-plane.internal",
        service_credential="service-secret",
        ca_file="ca.pem", client_cert_file="client.pem", client_key_file="client.key",
        gateway_assertion="gateway-secret", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HelloRedemptionError) as exc:
        await client.redeem(_hello())

    message = str(exc.value)
    assert "http=401" in message, message
    assert "code=40114" in message, f"必须带业务码才能归因，实测 {message!r}"
    assert "n/a" not in message, "不得把已知业务码降级成 n/a（那会毁掉归因能力）"
