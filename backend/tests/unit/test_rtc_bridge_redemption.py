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
