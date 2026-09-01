"""Marker 尾音不进下行：[STANDBY]/[ACTIVE] 文本标记对应的 TTS 尾音必须被丢弃。

真机实锤（2026-08-26）：APM 端到端模型把 "[STANDBY]"/"[ACTIVE]" 当文本念出来，
用户在"退下"/唤醒确认语结尾听到英文。文本侧 strip 只保护 Brain 路由，
音频流没有对应裁剪——本文件锁定 marker 后的音频必须全部丢弃。
"""
from __future__ import annotations

import pytest

from rtc_bridge.session import PeerVoiceSession


class StubApm:
    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, **kwargs) -> None:
        self.on_audio_out = on_audio_out
        self.on_text = on_text
        self.closed = False
        self.dead = False
        StubApm.instances.append(self)

    async def feed_pcm(self, pcm: bytes) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_session(monkeypatch, sent: list[dict] | None = None) -> PeerVoiceSession:
    monkeypatch.setattr("rtc_bridge.session.ApmBridge", StubApm)
    async def send(msg: dict) -> None:
        if sent is not None:
            sent.append(msg)
    async def route(_text: str) -> None:
        return None
    return PeerVoiceSession(
        device_id="dev", room_id="room", send_msg=send,
        apm_api_url="ws://fake", apm_system_prompt="prompt",
        on_voice_intent=route,
    )


@pytest.mark.asyncio
async def test_stanbyy_marker_tail_audio_dropped(monkeypatch):
    """[STANDBY] 标记出现后的下行音频（标记尾音）必须被丢弃，不再进手机。"""
    s = make_session(monkeypatch)
    await s.start()
    await s.on_peer_enter("user")
    # 正常回复音频应下行
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["down_frames"] == 1
    # 标记文本到达（marker 检测发生在 _on_text）
    await s._on_text("好的，我退下了 [STANDBY]")
    # 标记后的尾音必须全部丢弃
    await s._on_audio_out(b"\x00\x10" * 320)
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["down_frames"] == 1            # 没有新增下行帧
    assert s.stats.get("marker_tail_drops", 0) >= 1
    await s.close()


@pytest.mark.asyncio
async def test_active_marker_tail_audio_dropped(monkeypatch):
    """[ACTIVE] 标记后的下行音频（标记尾音）同样必须丢弃。"""
    s = make_session(monkeypatch)
    await s.start()
    await s.on_peer_enter("user")
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["down_frames"] == 1
    await s._on_text("我在 [ACTIVE]")
    await s._on_audio_out(b"\x00\x10" * 320)
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["down_frames"] == 1            # 没有新增下行帧
    assert s.stats.get("marker_tail_drops", 0) >= 1
    await s.close()


@pytest.mark.asyncio
async def test_marker_tail_drop_window_closes_after_silence(monkeypatch):
    """标记尾音丢弃窗口在正常静默结束（>600ms）后关闭，不误伤下一轮回复。"""
    s = make_session(monkeypatch)
    await s.start()
    await s.on_peer_enter("user")
    await s._on_text("好的，我退下了 [STANDBY]")
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["down_frames"] == 0
    # 静默超时 → 播报结束 → 丢弃窗口关闭
    s._last_down_ts = 0.0
    await s._check_down_speaking_over()
    assert s._marker_tail_drop is False
    # 下一轮正常回复音频不受影响（standby 态另行丢弃，此测试只验窗口关闭）
    s._standby = False
    s._standby_pending = False
    await s._on_audio_out(b"\x00\x10" * 320)
    assert s.stats["down_frames"] == 1
    await s.close()
