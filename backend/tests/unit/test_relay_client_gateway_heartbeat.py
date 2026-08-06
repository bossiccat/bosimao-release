"""relay_client 对语音网关的心跳应答单测（回归：未知控制帧类型 pong）

背景（2026-08-05 现场）：手机已连接，但语音网关报"未知控制帧类型: pong"。
链路：网关 _heartbeat_loop 每 30s 发 {"type":"ping"} → relay_client._down_control
回 {"type":"pong"} → 网关 CTRL_OK 不含 pong（上行合法保活帧是 heartbeat）→ 报错
且不刷新 last_rx → 心跳超时被踢。

协议真源（backend/app/voice/schemas.py）：
- 上行：hello / audio_start / audio_end / wake / cancel / heartbeat / speech_start / speech_end / interrupt
- 下行：ready / session_state / transcript / audio_start / audio_end / reply_done / error / pong

结论：relay_client 对网关的 ping 必须回 heartbeat（pong 是下行帧，只能网关→客户端）。
"""
from __future__ import annotations

import asyncio
import json

from relay.relay_client import RelayClient


class FakeWs:
    """记录所有 send 的假 WS（兼容 str/bytes）"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data) -> None:
        self.sent.append(data if isinstance(data, str) else data.decode(errors="replace"))


def _client() -> tuple[RelayClient, FakeWs, FakeWs]:
    c = RelayClient(
        relay_url="ws://127.0.0.1:1/relay/ws",  # 不实际连接，仅测逻辑
        token="tok",
        device_id="jax-pc-test",
        pairing_code="000000",
    )
    relay_ws, gw_ws = FakeWs(), FakeWs()
    c._relay_ws = relay_ws
    c._gw_ws = gw_ws
    return c, relay_ws, gw_ws


def test_gateway_ping_replies_heartbeat_not_pong():
    """网关 ping → relay_client 必须回 heartbeat（网关上行合法帧），绝不可回 pong"""
    c, relay_ws, gw_ws = _client()
    asyncio.run(c._down_control(json.dumps({"type": "ping", "ts": 123.0})))

    # 应答帧只发往网关（_gw_ws），不回中继
    assert relay_ws.sent == []
    assert len(gw_ws.sent) == 1
    reply = json.loads(gw_ws.sent[0])
    assert reply["type"] == "heartbeat", f"网关 ping 应答必须是 heartbeat，实际: {reply['type']}"
    assert "ts" in reply


def test_gateway_pong_not_forwarded_to_relay():
    """网关回执 pong（对 heartbeat 的应答）→ relay_client 吞掉，不透传中继"""
    c, relay_ws, gw_ws = _client()
    asyncio.run(c._down_control(json.dumps({"type": "pong", "ts": 123.0})))

    assert relay_ws.sent == []
    assert gw_ws.sent == []   # 不回应答帧（避免 ping/pong 死循环）
    assert c.stats["control"] == 0


def test_relay_pong_not_forwarded_to_gateway():
    """中继弹回的 pong（relay_client 回 pong 后中继拦截弹回，relay_server.py forward）
    → 必须丢弃，绝不可透传网关：否则网关报"未知控制帧类型: pong"→ 回 error →
    中继无 peer 再回 no_peer error → 15s 无限循环（2026-08-05 现场：relay_v5.err 每 15s 一条
    "relay event: error" + 手机 App 显示"服务器: 未知控制帧类型: pong"）"""
    c, relay_ws, gw_ws = _client()
    asyncio.run(c._up_control(json.dumps({"type": "pong", "ts": 123.0})))

    assert gw_ws.sent == []            # pong 绝不透传网关
    assert c.stats["control"] == 0
    # 中继弹回的 pong 无需再回 pong（应答语义已完成），只吞掉
    assert relay_ws.sent == []


def test_gateway_active_heartbeat_sent_periodically():
    """relay_client 必须主动向网关发 heartbeat（对齐手机端 15s 心跳），不能只被动应答：
    网关 heartbeat_timeout_s(15) < interval_s(30)，只靠"收到 ping 才回"必然每周期被误踢
    （2026-08-05 现场：jax.log 每 30s 一条 voice heartbeat timeout, closing jax-pc-01）"""
    c, relay_ws, gw_ws = _client()
    c.gateway_heartbeat_interval_s = 0.01   # 测试用快间隔

    async def run():
        task = asyncio.create_task(c._gateway_heartbeat())
        # 事件驱动等待第一条 heartbeat（不依赖固定 sleep 次数，避免 CPU 抖动误报）
        for _ in range(50):
            if gw_ws.sent:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert gw_ws.sent, "应主动向网关发送 heartbeat"
    for raw in gw_ws.sent:
        assert json.loads(raw)["type"] == "heartbeat"
    # 主动心跳只发往网关，不发中继
    assert relay_ws.sent == []


def test_normal_down_control_forwarded():
    """非心跳下行帧（如 reply_done）→ 原样转发给中继"""
    c, relay_ws, gw_ws = _client()
    asyncio.run(c._down_control(json.dumps({"type": "reply_done", "text": "ok"})))

    assert relay_ws.sent == [json.dumps({"type": "reply_done", "text": "ok"})]
    assert gw_ws.sent == []
    assert c.stats["control"] == 1
