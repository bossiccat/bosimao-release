"""Fail-closed one-shot Control Plane hello redemption client."""
from __future__ import annotations

import ssl
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class HelloRedemptionError(RuntimeError):
    """Control Plane rejected or could not redeem a sidecar hello."""


class AudioFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    encoding: Literal["pcm_s16le"]
    sample_rate_hz: Literal[16000]
    channels: Literal[1]
    frame_ms: Literal[20]
    frame_bytes: Literal[640]


class HelloPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hello"]
    proof: str = Field(min_length=1)
    nonce: str = Field(min_length=16, max_length=512)
    jti: str = Field(min_length=1)
    session_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=64)
    room_id: str = Field(min_length=1, max_length=64)
    sidecar_user_id: Literal["jax-pc-sidecar"]
    generation: int = Field(ge=0)
    protocol_version: Literal["1.0"]
    audio_format: AudioFormat


def validate_hello(value: object) -> dict:
    try:
        return HelloPayload.model_validate(value).model_dump()
    except ValidationError as exc:
        raise HelloRedemptionError("invalid hello") from exc


class HelloRedemptionClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_credential: str,
        ca_file: str,
        client_cert_file: str,
        client_key_file: str,
        gateway_assertion: str,
        connect_timeout_s: float = 0.5,
        total_timeout_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        required = (
            base_url, service_credential, ca_file, client_cert_file,
            client_key_file, gateway_assertion,
        )
        if any(not value for value in required) or not base_url.startswith("https://"):
            raise HelloRedemptionError("redemption client configuration unavailable")
        self._url = base_url.rstrip("/") + "/api/v1/voice/internal/rtc-bridge/hello-redeem"
        self._headers = {
            "Authorization": f"Bearer {service_credential}",
            "X-Internal-Gateway-Assertion": gateway_assertion,
        }
        self._timeout = httpx.Timeout(
            timeout=total_timeout_s, connect=connect_timeout_s,
            read=total_timeout_s, write=total_timeout_s, pool=connect_timeout_s,
        )
        # 显式构建 SSL 上下文：httpx 的 verify=<ca 文件> + cert=(crt, key) 快捷
        # 参数在本机 mTLS（Windows/uvicorn 栈）下会触发服务端静默断连
        # （任务 #26 冒烟实证）；显式 context 与 raw socket 行为一致。MockTransport
        # 不走 TLS，允许单测传入哑路径而不读取证书文件。
        self._ssl_context = None
        if transport is None:
            self._ssl_context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH, cafile=ca_file
            )
            self._ssl_context.load_cert_chain(client_cert_file, client_key_file)
        self._transport = transport

    async def redeem(self, hello: dict) -> dict:
        payload = validate_hello(hello)
        try:
            async with httpx.AsyncClient(
                verify=self._ssl_context,
                timeout=self._timeout,
                transport=self._transport,
                # 控制面为内部 mTLS 链路，禁止继承 HTTP(S)_PROXY 以免凭据/流量
                # 被导向环境代理；目标 URL 已在初始化时强制要求 https。
                trust_env=False,
            ) as client:
                response = await client.post(self._url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise HelloRedemptionError("redemption transport failed") from exc
        if response.status_code != 200:
            raise HelloRedemptionError("redemption rejected")
        try:
            body = response.json()
            data = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HelloRedemptionError("invalid redemption response") from exc
        identity = ("session_id", "device_id", "room_id", "sidecar_user_id", "generation")
        if body.get("code") != 0 or data.get("redeemed") is not True:
            raise HelloRedemptionError("redemption rejected")
        if any(data.get(key) != payload[key] for key in identity):
            raise HelloRedemptionError("redemption binding mismatch")
        if not isinstance(data.get("expires_at"), str) or not data["expires_at"]:
            raise HelloRedemptionError("invalid redemption response")
        return data
