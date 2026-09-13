"""Fail-closed one-shot Control Plane hello redemption client."""
from __future__ import annotations

import asyncio
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
    """hello 兑付客户端。

    ⚠️ 超时默认值（2026-09-12 修正）：原为 connect=0.5s / total=2.0s，那是
    「sidecar 与控制面**同机**」时代的调参。媒体面回到用户机器、控制面在公网后，
    沿用该值会把**成功的兑付判成失败**，而兑付失败是 fail-closed 终局
    （`server.py` 立即 `ctrl exit hello_redemption_failed`，永不建会话），
    线上表现为「随机连不上」。现默认放宽到 5s/15s；同机场景可用
    `RTC_BRIDGE_CONTROL_PLANE_CONNECT_TIMEOUT_S` / `..._TOTAL_TIMEOUT_S` 收紧。
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_credential: str,
        ca_file: str,
        client_cert_file: str,
        client_key_file: str,
        gateway_assertion: str,
        connect_timeout_s: float = 5.0,
        total_timeout_s: float = 15.0,
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
        # 2026-09-05：ConnectTimeout 单次安全重试——连接未建立=请求未发出=jti 未消费，
        # 重试无重放/双消费风险；ReadTimeout（请求已到达）绝不重试（40914 fail-closed）。
        # 背景：兑付恰逢 App sign 峰值时，backend accept 偶发 >0.5s（18:44:36 实证）。
        try:
            return await self._redeem_once(payload)
        except httpx.ConnectTimeout as first_exc:
            await asyncio.sleep(0.1)
            try:
                return await self._redeem_once(payload)
            except httpx.HTTPError as exc:
                raise HelloRedemptionError("redemption transport failed (retry)") from exc
        except httpx.HTTPError as exc:
            raise HelloRedemptionError("redemption transport failed") from exc

    async def _redeem_once(self, payload: dict) -> dict:
        # httpx.HTTPError 直接冒泡（redeem 统一分类：ConnectTimeout 可重试，其余包装 fail-closed）
        async with httpx.AsyncClient(
            verify=self._ssl_context,
            timeout=self._timeout,
            transport=self._transport,
            # 控制面为内部 mTLS 链路，禁止继承 HTTP(S)_PROXY 以免凭据/流量
            # 被导向环境代理；目标 URL 已在初始化时强制要求 https。
            trust_env=False,
        ) as client:
            response = await client.post(self._url, json=payload, headers=self._headers)
        if response.status_code != 200:
            # 只带**数字**：HTTP 状态 + 业务错误码。服务端返回的是锁定错误码，每一个
            # 精确对应一道不同的校验（40101=服务凭证不被接受 / 40114=断言哈希或证书绑定
            # 不一致 / 40021=会话状态）。旧实现把这两者统一吞成 "redemption rejected"，
            # 导致兑付失败完全无法归因 —— 实测为此白排查了多轮。
            code = "n/a"
            try:
                code = str((response.json() or {}).get("code", "n/a"))
            except (ValueError, TypeError):
                pass
            raise HelloRedemptionError(
                f"redemption rejected http={response.status_code} code={code}"
            )
        try:
            body = response.json()
            data = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HelloRedemptionError("invalid redemption response") from exc
        identity = ("session_id", "device_id", "room_id", "sidecar_user_id", "generation")
        if body.get("code") != 0 or data.get("redeemed") is not True:
            # 同上：带上业务码，否则 200 但业务拒绝时同样无法归因
            raise HelloRedemptionError(
                f"redemption rejected body_code={body.get('code')} "
                f"redeemed={data.get('redeemed')}"
            )
        if any(data.get(key) != payload[key] for key in identity):
            raise HelloRedemptionError("redemption binding mismatch")
        if not isinstance(data.get("expires_at"), str) or not data["expires_at"]:
            raise HelloRedemptionError("invalid redemption response")
        return data
