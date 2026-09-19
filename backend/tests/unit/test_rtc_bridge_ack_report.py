"""rtc_bridge 控制面 ack 上报（bridge_drained_closed）单测。

覆盖：
- AckReporterClient HTTP 契约（URL/头/payload/错误分支）
- DrainAcknowledger 生命周期语义（有终止上下文才上报、只报一次、失败不冒泡）
- BridgeServer drain 钩子（正常关闭/顶替/上报失败不影响主链路）
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import websockets

from rtc_bridge.config import BridgeConfig
from rtc_bridge.server import BridgeServer

# 本文件所有 websockets.connect 都打本机（127.0.0.1 上由测试自己起的 bridge/server）。
# `websockets` 的 `proxy` 默认是 `True`（= 按环境代理），设了 HTTP_PROXY 的机器上
# loopback 请求会被交给代理 ⇒ **服务活着却连不上**（同 docs/OPS-003-live-test.md:98-100）。
# 故每处显式 `proxy=None`。契约锁：backend/tests/contract/test_loopback_probe_proxy_contract.py

logging.disable(logging.WARNING)


# ---------- AckReporterClient ----------

class RecordingAckReporter:
    """测试替身：记录 report_drained_closed 调用"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def report_drained_closed(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"ack_result": "confirmed"}


class BoomAckReporter:
    async def report_drained_closed(self, **kwargs) -> dict:
        raise RuntimeError("control plane unreachable")


def _ack_client(transport: httpx.AsyncBaseTransport | None = None):
    from rtc_bridge.ack_reporter import AckReporterClient

    return AckReporterClient(
        base_url="https://control-plane.example",
        service_credential="bridge-secret",
        ca_file="ca.pem",
        client_cert_file="client.crt",
        client_key_file="client.key",
        gateway_assertion="assertion-token",
        transport=transport,
    )


@pytest.mark.asyncio
async def test_ack_client_posts_contract_payload():
    """POST 到 acknowledgements 端点；携带服务凭证/网关断言/nonce 头与契约字段"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            202, json={"code": 0, "data": {"ack_result": "pending"}, "message": ""}
        )

    client = _ack_client(httpx.MockTransport(handler))
    data = await client.report_drained_closed(
        termination_id="tid-1", session_id="session-001", device_id="dev-001",
        room_id="jax-dev-001", generation=3,
    )
    assert data == {"ack_result": "pending"}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == (
        "/api/v1/voice/sessions/session-001/termination/tid-1/acknowledgements"
    )
    assert req.headers["authorization"] == "Bearer bridge-secret"
    assert req.headers["x-internal-gateway-assertion"] == "assertion-token"
    assert len(req.headers["x-request-nonce"]) >= 16
    body = json.loads(req.content.decode("utf-8"))
    assert body == {
        "acknowledgement": "bridge_drained_closed",
        "result": "confirmed",
        "session_id": "session-001",
        "device_id": "dev-001",
        "room_id": "jax-dev-001",
        "generation": 3,
        "reported_at": body["reported_at"],
    }
    assert body["reported_at"].endswith("Z")
    assert "error_code" not in body


@pytest.mark.asyncio
async def test_ack_client_failed_result_carries_error_code():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202, json={"code": 0, "data": {}, "message": ""})

    client = _ack_client(httpx.MockTransport(handler))
    await client.report_drained_closed(
        termination_id="tid-1", session_id="s", device_id="d", room_id="r",
        generation=0, result="failed", error_code="bridge_close_failed",
    )
    body = json.loads(captured[0].content.decode("utf-8"))
    assert body["result"] == "failed"
    assert body["error_code"] == "bridge_close_failed"


@pytest.mark.asyncio
async def test_ack_client_rejects_contract_violations_locally():
    """result 与 error_code 组合不合法 → 本地拒绝（不触网）"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach network")

    client = _ack_client(httpx.MockTransport(handler))
    from rtc_bridge.ack_reporter import AckReportError

    with pytest.raises(AckReportError):
        await client.report_drained_closed(
            termination_id="t", session_id="s", device_id="d", room_id="r",
            generation=0, result="failed", error_code=None,
        )
    with pytest.raises(AckReportError):
        await client.report_drained_closed(
            termination_id="t", session_id="s", device_id="d", room_id="r",
            generation=0, result="confirmed", error_code="oops",
        )


@pytest.mark.asyncio
async def test_ack_client_maps_http_and_transport_failures():
    from rtc_bridge.ack_reporter import AckReportError

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": 40901, "data": None, "message": "x"})

    client = _ack_client(httpx.MockTransport(reject))
    with pytest.raises(AckReportError):
        await client.report_drained_closed(
            termination_id="t", session_id="s", device_id="d", room_id="r",
            generation=0,
        )

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client2 = _ack_client(httpx.MockTransport(broken))
    with pytest.raises(AckReportError):
        await client2.report_drained_closed(
            termination_id="t", session_id="s", device_id="d", room_id="r",
            generation=0,
        )


def test_ack_client_config_fail_closed():
    from rtc_bridge.ack_reporter import AckReportError

    with pytest.raises(AckReportError):
        _ack_client_base_url_empty()


def _ack_client_base_url_empty():
    from rtc_bridge.ack_reporter import AckReporterClient

    return AckReporterClient(
        base_url="", service_credential="x", ca_file="x",
        client_cert_file="x", client_key_file="x", gateway_assertion="x",
    )


# ---------- BridgeServer drain 钩子 ----------

class FakeRedemption:
    async def redeem(self, hello: dict) -> dict:
        return {
            "redeemed": True,
            **{key: hello[key] for key in (
                "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
            )},
            "expires_at": "2026-08-25T10:00:00Z",
        }


def _hello(session_id: str, device_id: str, room_id: str) -> dict:
    return {
        "type": "hello", "proof": f"proof-{session_id}",
        "nonce": f"nonce-{session_id}-current", "jti": f"jti-{session_id}",
        "session_id": session_id, "device_id": device_id, "room_id": room_id,
        "sidecar_user_id": "jax-pc-sidecar", "generation": 0,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }


async def _start_server(reporter):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    bridge = BridgeServer(cfg, state, redemption=FakeRedemption(), ack_reporter=reporter)
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return bridge, state, server, port


async def _connect_sidecar(port: str, session_id: str, device_id: str, room_id: str):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}", proxy=None)
    await ws.send(json.dumps(_hello(session_id, device_id, room_id)))
    ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert ready["type"] == "ready"
    return ws


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_drain_reports_bridge_drained_closed_when_context_present():
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-001", "dev-001", "jax-dev-001")
        bridge.note_termination("session-001", "tid-42")
        await ws.close()
        assert await _wait_for(lambda: len(reporter.calls) == 1)
        call = reporter.calls[0]
        assert call["termination_id"] == "tid-42"
        assert call["session_id"] == "session-001"
        assert call["device_id"] == "dev-001"
        assert call["room_id"] == "jax-dev-001"
        assert call["generation"] == 0
        assert call["result"] == "confirmed"
        assert call["error_code"] is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_drain_without_termination_context_skips_report():
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-001", "dev-001", "jax-dev-001")
        await ws.close()
        await asyncio.sleep(0.4)
        assert reporter.calls == []
        assert state["sidecar_connected"] is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_report_failure_never_breaks_voice_path_or_cleanup():
    """上报抛异常 → 清理仍完成、状态复位、后续新会话可正常接入"""
    reporter = BoomAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws = await _connect_sidecar(port, "session-a", "dev-a", "r-a")
        bridge.note_termination("session-a", "tid-a")
        await ws.close()
        await asyncio.sleep(0.3)
        assert state["sidecar_connected"] is False
        # 主链路未受影响：新 sidecar 可正常握手激活
        ws2 = await _connect_sidecar(port, "session-b", "dev-b", "r-b")
        assert state["sidecar_connected"] is True
        await ws2.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_replacement_reports_old_session_exactly_once():
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)
    try:
        ws_a = await _connect_sidecar(port, "session-a", "dev-a", "r-a")
        bridge.note_termination("session-a", "tid-a")
        ws_b = await _connect_sidecar(port, "session-b", "dev-b", "r-b")
        await asyncio.sleep(0.5)
        assert [call["session_id"] for call in reporter.calls].count("session-a") == 1
        assert all(call["session_id"] != "session-b" for call in reporter.calls)
        await ws_b.close()
        await ws_a.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unclean_session_close_reports_failed_with_error_code():
    """session.close 抛异常 → 上报 failed + error_code（不吞掉真实故障）"""
    reporter = RecordingAckReporter()
    bridge, state, server, port = await _start_server(reporter)

    import rtc_bridge.server as server_mod

    original_close = server_mod.PeerVoiceSession.close

    async def exploding_close(self):
        raise RuntimeError("apm wedged")

    server_mod.PeerVoiceSession.close = exploding_close
    try:
        ws = await _connect_sidecar(port, "session-x", "dev-x", "r-x")
        bridge.note_termination("session-x", "tid-x")
        await ws.close()
        assert await _wait_for(lambda: len(reporter.calls) == 1)
        call = reporter.calls[0]
        assert call["result"] == "failed"
        assert call["error_code"] == "bridge_session_close_failed"
    finally:
        server_mod.PeerVoiceSession.close = original_close
        server.close()
        await server.wait_closed()
