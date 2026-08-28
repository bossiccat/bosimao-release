"""Fail-safe Control Plane acknowledgement reporter for rtc_bridge.

AckReporterClient：HTTPS + mTLS 上报 acknowledgements（与 hello redemption
同一套控制面凭据派生：服务凭证 Bearer + 网关断言头 + 客户端证书）。
drain 生命周期语义（DrainAcknowledger）见 drain_ack.py。
"""
from __future__ import annotations

import ssl
import uuid
from datetime import datetime, timezone

import httpx

ACK_BRIDGE_DRAINED_CLOSED = "bridge_drained_closed"
ACK_APM_CANCELLED_CLOSED = "apm_cancelled_closed"


class AckReportError(RuntimeError):
    """Control Plane rejected or could not receive an acknowledgement report."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class AckReporterClient:
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
            raise AckReportError("ack reporter configuration unavailable")
        self._base_url = base_url.rstrip("/")
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

    async def report_drained_closed(
        self, *, termination_id: str, session_id: str, device_id: str,
        room_id: str, generation: int, result: str = "confirmed",
        error_code: str | None = None,
    ) -> dict:
        return await self._post(
            ACK_BRIDGE_DRAINED_CLOSED, termination_id=termination_id,
            session_id=session_id, device_id=device_id, room_id=room_id,
            generation=generation, result=result, error_code=error_code,
        )

    async def report_apm_cancelled_closed(
        self, *, termination_id: str, session_id: str, device_id: str,
        room_id: str, generation: int, result: str = "confirmed",
        error_code: str | None = None,
    ) -> dict:
        return await self._post(
            ACK_APM_CANCELLED_CLOSED, termination_id=termination_id,
            session_id=session_id, device_id=device_id, room_id=room_id,
            generation=generation, result=result, error_code=error_code,
        )

    async def _post(
        self, acknowledgement: str, *, termination_id: str, session_id: str,
        device_id: str, room_id: str, generation: int, result: str,
        error_code: str | None,
    ) -> dict:
        if result not in {"confirmed", "failed"}:
            raise AckReportError("invalid acknowledgement result")
        if result == "failed" and not error_code:
            raise AckReportError("error_code is required when result is failed")
        if result == "confirmed" and error_code is not None:
            raise AckReportError("error_code must be omitted when result is confirmed")
        url = (
            f"{self._base_url}/api/v1/voice/sessions/{session_id}"
            f"/termination/{termination_id}/acknowledgements"
        )
        payload: dict = {
            "acknowledgement": acknowledgement,
            "result": result,
            "session_id": session_id,
            "device_id": device_id,
            "room_id": room_id,
            "generation": generation,
            "reported_at": _utc_now_iso(),
        }
        if error_code is not None:
            payload["error_code"] = error_code
        headers = {**self._headers, "X-Request-Nonce": uuid.uuid4().hex}
        try:
            async with httpx.AsyncClient(
                verify=self._ssl_context,
                timeout=self._timeout,
                transport=self._transport,
                # 控制面为内部 mTLS 链路，禁止继承 HTTP(S)_PROXY 以免凭据/流量
                # 被导向环境代理；目标 URL 已在初始化时强制要求 https。
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AckReportError("ack report transport failed") from exc
        if response.status_code not in (200, 202):
            raise AckReportError("ack report rejected")
        try:
            body = response.json()
        except ValueError as exc:
            raise AckReportError("invalid ack report response") from exc
        if body.get("code") != 0:
            raise AckReportError("ack report rejected")
        data = body.get("data")
        return data if isinstance(data, dict) else {}
