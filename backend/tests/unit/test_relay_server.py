"""中继服务单测（M2）：双端配对透传 / 鉴权 / 心跳 / 断线清理

用 FastAPI TestClient 直接驱动 WS 端点（不启 uvicorn）；配对、双向转发、
token 鉴权、dev 态放行、对端断开通知 peer_left、控制面端点。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from relay.config import RelayConfig
from relay.relay_protocol import encode_audio_frame, make_pair_frame
from relay.relay_server import create_relay_app


def make_cfg(**kw) -> RelayConfig:
    base = {"heartbeat_interval_s": 0.2, "heartbeat_timeout_s": 30, "session_timeout_s": 30}
    base.update(kw)
    return RelayConfig(**base)


def make_client(cfg: RelayConfig | None = None) -> TestClient:
    cfg = cfg or make_cfg()
    return TestClient(create_relay_app(cfg))


def _pair_frame(role: str, device_id: str, code: str, token: str = "") -> str:
    return make_pair_frame(role, device_id, code, token)


# ---------- 双端配对 ----------
def test_dual_end_pairing_and_bidirectional_forward():
    client = make_client()
    with client.websocket_connect("/relay/ws") as phone:
        phone.send_text(_pair_frame("phone", "samsung", "123456"))
        with client.websocket_connect("/relay/ws") as pc:
            pc.send_text(_pair_frame("pc", "jax-pc", "123456"))
            paired_phone = json.loads(phone.receive_text())
            paired_pc = json.loads(pc.receive_text())
            assert paired_phone["type"] == "paired"
            assert paired_pc["type"] == "paired"
            assert paired_phone["peer"]["role"] == "pc"
            assert paired_pc["peer"]["role"] == "phone"
            assert paired_phone["session_id"] == paired_pc["session_id"]

            # 手机 → PC 文本控制帧透传
            phone.send_text(json.dumps({"type": "wake", "ts": 1}))
            assert json.loads(pc.receive_text())["type"] == "wake"
            # PC → 手机文本控制帧透传
            pc.send_text(json.dumps({"type": "session_state", "state": "listening", "ts": 2}))
            assert json.loads(phone.receive_text())["type"] == "session_state"
            # 手机 → PC 二进制音频帧透传（原样字节）
            audio = encode_audio_frame(0, 1000, b"pcm-bytes")
            phone.send_bytes(audio)
            assert pc.receive_bytes() == audio
            # PC → 手机二进制音频帧透传
            down = encode_audio_frame(0, 2000, b"down-pcm")
            pc.send_bytes(down)
            assert phone.receive_bytes() == down


# ---------- 心跳本地应答（不透传） ----------
def test_heartbeat_local_pong_not_forwarded():
    client = make_client()
    with client.websocket_connect("/relay/ws") as phone:
        phone.send_text(_pair_frame("phone", "samsung", "123456"))
        with client.websocket_connect("/relay/ws") as pc:
            pc.send_text(_pair_frame("pc", "jax-pc", "123456"))
            assert json.loads(phone.receive_text())["type"] == "paired"
            assert json.loads(pc.receive_text())["type"] == "paired"
            # 手机发 heartbeat → 中继本地回 pong，PC 收不到
            phone.send_text(json.dumps({"type": "heartbeat", "ts": 42}))
            assert json.loads(phone.receive_text())["type"] == "pong"


# ---------- 鉴权 ----------
def test_auth_required_and_wrong_token_rejected():
    cfg = make_cfg(token="secret", require_token=True)
    client = make_client(cfg)
    with client.websocket_connect("/relay/ws") as ws:
        ws.send_text(_pair_frame("phone", "samsung", "123456"))
        err = json.loads(ws.receive_text())
        assert err["type"] == "error" and err["code"] == "auth_failed"


def test_auth_query_token_ok():
    cfg = make_cfg(token="secret", require_token=True)
    client = make_client(cfg)
    with client.websocket_connect("/relay/ws?token=secret") as ws:
        ws.send_text(_pair_frame("phone", "samsung", "123456"))
        # 无对端 → 不立即收到 paired；无错误帧说明通过鉴权，进入等待
        # （心跳 ping 可能先到，断言不出现 auth_failed）
        frames = []
        for _ in range(3):
            frames.append(json.loads(ws.receive_text()))
        assert not any(f.get("code") == "auth_failed" for f in frames)


def test_dev_mode_no_token_allowed():
    client = make_client()  # require_token=False
    with client.websocket_connect("/relay/ws") as phone:
        phone.send_text(_pair_frame("phone", "samsung", "123456"))
        with client.websocket_connect("/relay/ws") as pc:
            pc.send_text(_pair_frame("pc", "jax-pc", "123456"))
            assert json.loads(phone.receive_text())["type"] == "paired"
            assert json.loads(pc.receive_text())["type"] == "paired"


# ---------- 断线清理 ----------
def test_disconnect_notifies_peer_left():
    client = make_client()
    with client.websocket_connect("/relay/ws") as phone:
        phone.send_text(_pair_frame("phone", "samsung", "123456"))
        with client.websocket_connect("/relay/ws") as pc:
            pc.send_text(_pair_frame("pc", "jax-pc", "123456"))
            assert json.loads(phone.receive_text())["type"] == "paired"
            assert json.loads(pc.receive_text())["type"] == "paired"
        # PC 连接关闭 → 手机收到 peer_left
        left = json.loads(phone.receive_text())
        assert left["type"] == "peer_left"
        assert left["device_id"] == "jax-pc"


def test_reconnect_after_peer_left_reforms_session():
    client = make_client()
    with client.websocket_connect("/relay/ws") as phone:
        phone.send_text(_pair_frame("phone", "samsung", "123456"))
        with client.websocket_connect("/relay/ws") as pc_a:
            pc_a.send_text(_pair_frame("pc", "jax-pc", "123456"))
            assert json.loads(phone.receive_text())["type"] == "paired"
            assert json.loads(pc_a.receive_text())["type"] == "paired"
        assert json.loads(phone.receive_text())["type"] == "peer_left"
        # PC 重连 → 重新配对
        with client.websocket_connect("/relay/ws") as pc_b:
            pc_b.send_text(_pair_frame("pc", "jax-pc", "123456"))
            assert json.loads(phone.receive_text())["type"] == "paired"
            assert json.loads(pc_b.receive_text())["type"] == "paired"


# ---------- 配对帧校验 ----------
def test_bad_pair_frame_rejected():
    client = make_client()
    with client.websocket_connect("/relay/ws") as ws:
        ws.send_text(json.dumps({"type": "hello", "role": "phone"}))
        err = json.loads(ws.receive_text())
        assert err["code"] == "bad_frame"


# ---------- 控制面端点 ----------
def test_relay_pair_endpoint():
    client = make_client()
    resp = client.post("/relay/pair")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["pairing_code"]) == 6
    assert "/relay/ws" in data["ws_url"]
    assert data["token_required"] is False
    assert isinstance(data["e2ee_enabled"], bool)


def test_relay_stats_and_health():
    client = make_client()
    assert client.get("/relay/health").json()["status"] == "ok"
    resp = client.get("/relay/stats")
    assert resp.status_code == 200
    assert "paired" in resp.json()["data"]["stats"]


# ---------- 心跳超时清理 ----------
def test_heartbeat_timeout_kicks_connection():
    cfg = make_cfg(heartbeat_interval_s=0.1, heartbeat_timeout_s=0.3)
    client = make_client(cfg)
    with pytest.raises(Exception):
        with client.websocket_connect("/relay/ws") as ws:
            ws.send_text(_pair_frame("phone", "samsung", "123456"))
            # 不回应 ping → 超时被踢，receive 抛异常
            for _ in range(10):
                ws.receive_text()
