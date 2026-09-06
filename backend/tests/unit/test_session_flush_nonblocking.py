"""P0（2026-09-06 21:06 真机实锤）：flush 不得阻塞上行消费循环

故障链：AI 说完 → _consume_up 每轮开头的 _check_down_speaking_over 同步
`await self._router.flush()` → flush 走 on_voice_intent → brain API /intent
超时 5~8s → 上行循环整体停摆 → 用户语音 6~8s 不上行（"讲话卡断像网络差"）；
同时 qwen 下行音频积压，解除后突发到达被 DownlinkShaper 丢超龄帧 → 应答砍断。

覆盖：
1. AI 说完触发 flush 窗口后，brain 回调挂起期间 apm.feed_pcm 必须继续被调用
   （基线实现会阻塞到 flush 结束）
2. router buffer 满触发自动 flush 时，feed() 立即返回不被 brain 调用阻塞
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.brain.voice_intent_router import VoiceIntentRouter
from rtc_bridge.session import PeerVoiceSession


class StubApm:
    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, on_state=None,
                 on_error=None, api_url="", system_prompt="", token="") -> None:
        self.fed: list[bytes] = []
        self.closed = False
        self.dead = False
        StubApm.instances.append(self)

    async def feed_pcm(self, pcm: bytes) -> None:
        if self.closed or self.dead:
            return
        self.fed.append(pcm)

    async def close(self) -> None:
        self.closed = True

    async def start(self) -> None:
        pass


@pytest.fixture
def stub_apm(monkeypatch):
    StubApm.instances = []
    monkeypatch.setattr("rtc_bridge.session.ApmBridge", StubApm)
    return StubApm.instances


async def _sent(msg: dict) -> None:
    pass


# ---------- 1. AI 说完 flush 窗口：brain 挂起不得阻塞上行循环 ----------

@pytest.mark.asyncio
async def test_flush_hang_does_not_block_uplink_loop(stub_apm):
    release = asyncio.Event()
    routed: list[str] = []

    async def slow_route(text: str) -> None:
        routed.append(text)
        try:  # 模拟 brain API /intent 超时挂起（基线实测 5~8s）
            await asyncio.wait_for(release.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    s = PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        on_voice_intent=slow_route,
    )
    await s.start()
    await s.on_peer_enter("user-1")

    # AI 说话 + 文本 delta
    await s._on_audio_out(b"\x00\x10" * 320)
    await s._on_text("AI 的回复文本")
    # 静默判定满足 → consumer 下轮循环顶部触发 flush 窗口
    s._last_down_ts = 0.0

    # 关键断言：flush 挂起期间，上行帧仍被消费（apm.feed_pcm 继续被调用）
    t0 = time.monotonic()
    await s.on_up_audio(b"\x00\x00" * 320)
    fed_at = None
    while time.monotonic() - t0 < 0.5:
        if StubApm.instances[0].fed:
            fed_at = time.monotonic() - t0
            break
        await asyncio.sleep(0.01)
    assert fed_at is not None, "上行帧应被消费（基线：flush 挂起 2s，上行停摆）"
    assert fed_at < 0.2, f"上行帧应在 ~100ms 内被消费，实际 {fed_at:.3f}s（基线阻塞到 flush 结束）"

    # flush 最终仍应路由完整文本（异步化不丢功能）
    release.set()
    for _ in range(50):
        if routed:
            break
        await asyncio.sleep(0.01)
    assert routed and "AI 的回复文本" in routed[0]
    assert s._router.buffered_text == "", "flush 后 buffer 应清空"
    await s.close()


# ---------- 2. router 自动 flush：feed() 调用方不被 brain 挂起阻塞 ----------

@pytest.mark.asyncio
async def test_router_autoflush_does_not_block_feed():
    release = asyncio.Event()
    routed: list[str] = []

    async def slow_route(text: str) -> None:
        routed.append(text)
        try:
            await asyncio.wait_for(release.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    router = VoiceIntentRouter(on_route=slow_route, max_buffer=50)
    t0 = time.monotonic()
    await router.feed("A" * 50)  # 达到 max_buffer 触发自动 flush
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2, (
        f"自动 flush 不应阻塞 feed 调用方（recv 链路 20ms 级节奏），"
        f"实际阻塞 {elapsed:.3f}s"
    )

    release.set()
    for _ in range(50):
        if routed:
            break
        await asyncio.sleep(0.01)
    assert routed == ["A" * 50], "自动 flush 最终仍应路由完整文本"
    assert router.buffered_text == "", "自动 flush 应清空 buffer"
