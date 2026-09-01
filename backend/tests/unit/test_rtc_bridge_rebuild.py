"""A9：PeerVoiceSession peer leave → 重进房桥重建测试（不触网）

覆盖审计实锤：peer leave 关死 ApmBridge（_closed=True 不可逆）后，TRTC 断线重连
（SDK 内置，手机无感知）远端重进房 → 旧实现 feed 全被吞 → 永久静音。
"""
from __future__ import annotations

import asyncio
import base64
import json

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


def _make_session() -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
    )


@pytest.mark.asyncio
async def test_peer_reenter_after_leave_rebuilds_bridge(stub_apm):
    """A9 核心：leave（桥关死）→ enter → 必须重建新桥，feed 不被吞"""
    s = _make_session()
    await s.start()
    apm1 = s.apm
    await s.on_peer_enter("user-1")
    await s.on_up_audio(b"\x01\x02" * 320)
    await asyncio.sleep(0.05)
    assert apm1.fed, "首会话上行应到达 apm"

    # peer leave → 桥关死（旧实现不可逆）
    await s.on_peer_leave("user-1")
    assert apm1.closed is True
    await s.on_up_audio(b"\x03\x04" * 320)   # 关死后 feed 应被旧桥吞掉（close 后静默）
    n1 = len(apm1.fed)

    # TRTC 重连 → 远端重进房：必须重建（旧实现：同实例 → 永久静音）
    await s.on_peer_enter("user-1")
    apm2 = s.apm
    assert apm2 is not apm1, "重进房必须重建 ApmBridge（旧实例 _closed 不可逆）"
    assert apm2.closed is False
    await s.on_up_audio(b"\x05\x06" * 320)
    await asyncio.sleep(0.05)
    assert apm2.fed, "重进房后上行必须到达新桥（修复前：永久静音）"
    assert len(apm1.fed) == n1
    await s.close()


@pytest.mark.asyncio
async def test_dead_bridge_rebuilt_on_peer_enter(stub_apm):
    """A8+A9 协同：桥进入 dead 终态（重连放弃）→ 重进房同样重建"""
    s = _make_session()
    await s.start()
    s.apm.dead = True   # 模拟 A8 调度器放弃
    await s.on_peer_enter("user-1")
    assert s.apm.dead is False
    assert s.apm is not StubApm.instances[0]
    await s.close()


@pytest.mark.asyncio
async def test_live_bridge_not_rebuilt_on_peer_enter(stub_apm):
    """桥健康时重进房：不重建（避免无谓断开正在用的云会话），只重置判定器"""
    s = _make_session()
    await s.start()
    await s.on_peer_enter("user-1")
    apm1 = s.apm
    await s.on_peer_enter("user-1")   # 二次 enter（同桥健康）
    assert s.apm is apm1
    await s.close()


@pytest.mark.asyncio
async def test_apm_error_notified_via_ctrl(stub_apm):
    """A8 错误上报链路：on_error → session._on_apm_error → sidecar ctrl apm_error"""
    sent: list[dict] = []

    async def send(msg: dict) -> None:
        sent.append(msg)

    s = PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=send,
        apm_api_url="ws://fake", apm_system_prompt="p",
    )
    await s.start()
    await s.apm.on_error("apm_reconnect_gave_up", "云端引擎断开")
    await asyncio.sleep(0.05)
    assert any(
        m.get("type") == "ctrl" and m.get("action") == "apm_error"
        and m.get("reason") == "apm_reconnect_gave_up"
        for m in sent
    ), f"应向 sidecar 发 ctrl apm_error，实际 {sent}"
    await s.close()


@pytest.mark.asyncio
async def test_stale_uplink_flushed_on_rebuild(stub_apm):
    """重建时清空上行队列：断连期间堆积的旧帧不串入新会话"""
    s = _make_session()
    await s.start()
    apm1 = s.apm
    await s.on_peer_enter("user-1")
    await s.on_peer_leave("user-1")
    # 关死后先堆一批帧（旧桥吞掉，但入队了）
    for _ in range(5):
        await s.on_up_audio(b"\x07\x08" * 320)
    await asyncio.sleep(0.05)
    await s.on_peer_enter("user-1")   # 重建 + flush
    apm2 = s.apm
    assert apm2 is not apm1
    assert s._up_q.metrics()["queue_depth"] == 0
    await s.close()
