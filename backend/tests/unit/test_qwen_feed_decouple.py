"""feed_pcm 上行解耦（RED 先行）

现状：qwen_realtime_bridge.feed_pcm 每帧 await 云端 WS send（每秒 50 帧）。
云端网络一卡，20ms 高频上行消费循环整体被拖住 → BoundedAudioQueue 帧积压
→ 超 DEFAULT_UP_MAX_FRAME_AGE_MS=1000ms 整批判过期丢弃 → 用户「吞话、
像网络很差」。真机证据：14:21-14:23 会话 up rms 高、drops=0 但云端零事件
后的自恢复窗口；uplink 吞话假设自 2026-09-07 起挂账待验证。

修复契约：
1. feed_pcm 只入有界队列立即返回（慢网不阻塞上行消费循环）
2. 独立 sender task 串行消化队列（保序）
3. 队列满：丢最旧、保最新（与 BoundedAudioQueue 丢旧保新同一哲学），计数可见
4. sender 遇断链：丢帧计数 + 退避重连（与旧行为一致）
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

import app.voice.qwen_realtime_bridge as qb


class SlowWs:
    """send 可配置延迟、记录全部 send 的 WS 替身（握手按 session.updated 脚本走）"""

    def __init__(self, send_delay_s: float) -> None:
        self.send_delay_s = send_delay_s
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw) -> None:
        msg = json.loads(raw)
        if msg["type"] == "session.update":
            self.incoming.put_nowait(json.dumps(
                {"type": "session.updated", "session": {"id": "sid-1"}}))
            self.sent.append(msg)
            return
        await asyncio.sleep(self.send_delay_s)
        self.sent.append(msg)

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


def _pcm(i: int) -> bytes:
    return bytes([i]) * 320


async def _noop(_pcm: bytes) -> None:
    pass


@pytest.mark.asyncio
async def test_feed_pcm_does_not_block_on_slow_ws(monkeypatch):
    """契约 1：慢网下 feed_pcm 立即返回，不把上行消费循环拖住"""
    ws = SlowWs(send_delay_s=0.08)  # 每帧 send 80ms；3 帧 = 旧行为 240ms

    async def fake_connect_ws(*a, **k):
        return ws

    monkeypatch.setattr(qb, "connect_ws", fake_connect_ws)
    b = qb.QwenRealtimeBridge(on_audio_out=_noop, api_url="u", token="t")

    t0 = time.monotonic()
    for i in range(3):
        await asyncio.wait_for(b.feed_pcm(_pcm(i)), timeout=0.05)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.15, (
        f"feed_pcm 被云端 send 阻塞（3 帧耗 {elapsed:.3f}s）——"
        "旧行为每帧 await send，慢网时上行消费循环被拖死导致吞话"
    )

    # sender task 最终按序消化全部帧
    for _ in range(50):
        if len([m for m in ws.sent if m.get("type") == "input_audio_buffer.append"]) >= 3:
            break
        await asyncio.sleep(0.02)
    assert [m["audio"] for m in ws.sent
            if m.get("type") == "input_audio_buffer.append"] == [
        base64.b64encode(_pcm(i)).decode() for i in range(3)
    ], "解耦后必须保序投递"

    await b.close()


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest_keeps_newest(monkeypatch):
    """契约 3：队列满丢最旧保最新，计数可见"""
    ws = SlowWs(send_delay_s=0.05)

    async def fake_connect_ws(*a, **k):
        return ws

    monkeypatch.setattr(qb, "connect_ws", fake_connect_ws)
    b = qb.QwenRealtimeBridge(on_audio_out=_noop, api_url="u", token="t",
                              send_queue_frames=5)

    # 灌 20 帧（远超队列 5），慢 send 来不及消化
    for i in range(20):
        await b.feed_pcm(_pcm(i))

    assert b.dropped_frames > 0, "队列溢出必须有丢弃计数"

    # 等 sender 消化完：最终发出的必须是【最新】的帧（丢旧保新）
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(ws.sent) < 6:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)

    sent_audio = [base64.b64decode(m["audio"]) for m in ws.sent
                  if m.get("type") == "input_audio_buffer.append"]
    assert sent_audio, "sender 必须持续消化队列"
    assert sent_audio[-1] == _pcm(19), (
        "最后发出的必须是最新帧——丢旧保新，用户的最新语音不能丢"
    )
    assert len(sent_audio) <= 5, "发出的帧数不得超过队列容量（旧帧已被丢弃）"

    await b.close()
