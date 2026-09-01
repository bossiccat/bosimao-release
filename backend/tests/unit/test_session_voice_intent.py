"""PeerVoiceSession 集成测试 —— APM 文本路由到 Brain + barge-in 清空

验证"伪智能体"修复：
- AI 说完后（音频静默 600ms），累积的 AI 文本 flush 到 Brain 回调
- Barge-in 中断时，AI 部分文本被丢弃不路由
- 重进房时 router 清空（新会话干净起步）
"""
from __future__ import annotations

import asyncio

import pytest

from rtc_bridge.session import PeerVoiceSession


class StubApm:
    """可显式置 closed/dead 的 ApmBridge 替身"""

    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, on_state=None,
                 on_error=None, api_url="", system_prompt="", token="") -> None:
        self.on_audio_out = on_audio_out
        self.on_text = on_text
        self.on_state = on_state
        self.on_error = on_error
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


def _make_session(on_voice_intent=None) -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        on_voice_intent=on_voice_intent,
    )


@pytest.mark.asyncio
async def test_ai_text_routed_on_silence(stub_apm):
    """AI 说完后（静默 600ms+），累积文本 flush 到 Brain 回调"""
    intents: list[str] = []

    async def on_voice_intent(text: str) -> None:
        intents.append(text)

    s = _make_session(on_voice_intent=on_voice_intent)
    await s.start()
    await s.on_peer_enter("user-1")

    # 模拟 AI 说话：下行音频帧（设置 _down_speaking = True + _last_down_ts）
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s._down_speaking is True

    # 模拟 AI 文本 delta（APM response.output.delta kind=text）
    await s._on_text("好的，我来帮你")
    await s._on_text("重构这段代码。")
    assert s._router is not None
    assert "重构" in s._router.buffered_text

    # 模拟音频静默 600ms+：强制 _last_down_ts 为远古时间
    s._last_down_ts = 0.0
    await s._check_down_speaking_over()

    assert len(intents) == 1, f"应路由一次完整文本，实际 {intents}"
    assert "重构" in intents[0]
    assert s._router.buffered_text == "", "flush 后 buffer 应清空"
    await s.close()


@pytest.mark.asyncio
async def test_barge_in_clears_router(stub_apm):
    """Barge-in 中断时，AI 部分文本不路由到 Brain"""
    intents: list[str] = []

    async def on_voice_intent(text: str) -> None:
        intents.append(text)

    s = _make_session(on_voice_intent=on_voice_intent)
    await s.start()
    await s.on_peer_enter("user-1")

    # AI 开始说话
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s._down_speaking is True

    # AI 文本 delta（部分回复）
    await s._on_text("正在生成的旧回复文本")

    # 用户高能量帧打断（RMS > 800）
    # 0x4000 = 16384，RMS ≈ 16384，远超 800
    loud_pcm = b"\x00\x40" * 320
    await s.on_up_audio(loud_pcm)
    await asyncio.sleep(0.05)

    assert s._barge_in is True, "应触发 barge-in"
    assert s._router is not None
    assert s._router.buffered_text == "", "barge-in 应清空 router"

    # barge-in 后 AI 文本 delta 应被丢弃
    await s._on_text("barge-in 后的残余文本")
    assert s._router.buffered_text == "", "barge-in 后文本不应入 buffer"

    # 静默后 _check_down_speaking_over 不路由（buffer 为空）
    s._last_down_ts = 0.0
    await s._check_down_speaking_over()
    assert len(intents) == 0, "barge-in 中断的文本不应路由到 Brain"
    await s.close()


@pytest.mark.asyncio
async def test_peer_enter_clears_router(stub_apm):
    """重进房时清空 router（新会话干净起步，防跨会话串文本）"""
    intents: list[str] = []

    async def on_voice_intent(text: str) -> None:
        intents.append(text)

    s = _make_session(on_voice_intent=on_voice_intent)
    await s.start()
    await s.on_peer_enter("user-1")

    # 累积旧会话文本
    await s._on_text("旧会话的 AI 回复文本")
    assert s._router is not None
    assert len(s._router.buffered_text) > 0

    # 重进房
    await s.on_peer_enter("user-1")
    assert s._router.buffered_text == "", "重进房应清空 router"
    await s.close()


@pytest.mark.asyncio
async def test_no_brain_callback_still_logs(stub_apm):
    """无 on_voice_intent 时（Brain 不可用），文本仍入 router 但 flush 不报错"""
    s = _make_session()  # 无 on_voice_intent
    await s.start()
    await s.on_peer_enter("user-1")

    await s._on_audio_out(b"\x00\x10" * 320)
    await s._on_text("AI 回复")

    s._last_down_ts = 0.0
    await s._check_down_speaking_over()  # 不应抛异常
    await s.close()
