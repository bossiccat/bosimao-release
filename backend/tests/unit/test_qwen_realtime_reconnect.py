"""P0（2026-09-06 真机实锤）：QwenRealtimeBridge 断线自愈测试（mock ws，不触网）

故障链：云端 180s 空闲超时（code=response_idle_timeout）→ recv 1007 → 旧实现
recv 循环退出后零重连、_ws 保持死对象 → 手机端 20ms 高频喂帧全部
`feed apm failed: received 1007` 刷爆日志（单日 81302 条），用户体感"说了不理我"。

覆盖：
1. recv 循环异常退出（非 close() 主动关闭）→ 指数退避自动重连（重带
   system_prompt/tools 重建云端会话）→ feed 恢复
2. 重连窗口 feed_pcm 丢帧不抛异常 + 节流日志（≥2s 一条，不再每帧刷屏）
3. 连续失败达上限 → 放弃 → on_error 上报（含最终错误）→ dead 终态
4. session.py 接线：on_error → 结构化 WARNING cloud_engine_down + ctrl 上报
5. session.py `feed apm failed` 日志节流 ≥2s
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

import app.voice.qwen_realtime_bridge as qb
from app.voice.qwen_realtime_bridge import (
    QwenRealtimeBridge,
    QWEN_COORDINATION_TOOLS,
)
from rtc_bridge.session import PeerVoiceSession


# ---------- 工具 ----------

class FakeWs:
    """模拟 Qwen realtime ws：session.update 握手 + 可控 recv/send 抛错"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.recv_error: Exception | None = None

    async def send(self, raw) -> None:
        if self.closed:
            raise ConnectionError("send on closed ws")
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "session.update":
            self.incoming.put_nowait(
                json.dumps({"type": "session.updated", "session": {"id": "sid"}})
            )

    async def recv(self) -> str:
        if self.recv_error is not None:
            raise self.recv_error
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


async def _noop(_audio) -> None:
    return None


def _fast_scheduler(monkeypatch, delay: float = 0.005, max_attempts: int = 1000) -> None:
    """压缩退避时间，避免测试真实等待 1s~60s（强制覆盖 bridge 显式传参）"""
    real_init = qb.ReconnectScheduler.__init__

    def fast_init(self, attempt, on_give_up, on_retry=None, **_kwargs):
        real_init(self, attempt, on_give_up, on_retry, delay, delay, max_attempts)

    monkeypatch.setattr(qb.ReconnectScheduler, "__init__", fast_init)


async def _sent(msg: dict) -> None:
    pass


def _make_session(**kwargs) -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        **kwargs,
    )


# ---------- 1. recv 循环异常退出 → 自动重连 → feed 恢复 ----------

@pytest.mark.asyncio
async def test_recv_loop_exit_triggers_reconnect_and_feed_recovers(monkeypatch):
    ws1, ws2 = FakeWs(), FakeWs()
    connects: list[tuple[str, str, str, list]] = []

    async def fake_connect(api_url, token, system_prompt, tools):
        connects.append((api_url, token, system_prompt, list(tools)))
        if len(connects) == 1:
            return ws1, "s1"
        return ws2, "s2"

    monkeypatch.setattr(qb, "connect_qwen", fake_connect)
    _fast_scheduler(monkeypatch)

    bridge = QwenRealtimeBridge(on_audio_out=_noop, api_url="ws://fake",
                                token="tok", system_prompt="be a cat")
    await bridge.start()
    assert bridge._ws is ws1

    # 模拟云端 180s idle 超时后服务端关链路：recv 抛 1007
    ws1.recv_error = ConnectionResetError("received 1007 (abnormal closure)")

    for _ in range(400):
        if bridge._ws is ws2:
            break
        await asyncio.sleep(0.005)
    assert bridge._ws is ws2, "recv 循环异常退出后应自动重连（旧实现永久退出零重连）"
    assert bridge.reconnects == 1

    # 重连必须重带系统提示词与工具配置（新会话 re-sync）
    assert connects[1][2] == "be a cat"
    assert connects[1][3] == QWEN_COORDINATION_TOOLS

    # feed 恢复：上行帧到达新链路（2026-09-07 解耦后发送走 sender task，等其消化）
    await bridge.feed_pcm(b"\x01\x02" * 100)
    for _ in range(100):
        if any(m["type"] == "input_audio_buffer.append" for m in ws2.sent):
            break
        await asyncio.sleep(0.01)
    assert any(m["type"] == "input_audio_buffer.append" for m in ws2.sent)
    await bridge.close()


# ---------- 2. 重连窗口 feed_pcm 丢帧不抛异常 + 节流日志 ----------

@pytest.mark.asyncio
async def test_feed_pcm_drops_silently_while_link_down(monkeypatch, caplog):
    ws1 = FakeWs()
    connects = {"n": 0}

    async def fake_connect(api_url, token, system_prompt, tools):
        if not connects["n"]:
            connects["n"] = 1
            return ws1, "s1"
        raise ConnectionError("cloud down")

    monkeypatch.setattr(qb, "connect_qwen", fake_connect)
    _fast_scheduler(monkeypatch, max_attempts=1000)  # 不让测试窗口内放弃

    bridge = QwenRealtimeBridge(on_audio_out=_noop, api_url="ws://fake")
    await bridge.start()
    ws1.recv_error = ConnectionResetError("received 1007")
    for _ in range(400):
        if bridge._ws is None:
            break
        await asyncio.sleep(0.005)
    assert bridge._ws is None  # 已进入断线窗口

    with caplog.at_level(logging.WARNING, logger="app.voice.qwen_realtime_bridge"):
        for _ in range(20):          # 20ms 高频路径：绝不抛异常
            await bridge.feed_pcm(b"\x00\x00" * 32)
    assert bridge.dropped_frames == 20, "断线窗口帧应丢弃并计数"

    drop_logs = [r for r in caplog.records if "drop uplink frame" in r.getMessage()]
    assert len(drop_logs) <= 1, f"断线窗口 20 帧只应至多 1 条节流日志，实际 {len(drop_logs)} 条"
    await bridge.close()


# ---------- 3. 连续失败达上限 → 放弃 → on_error 上报 ----------

@pytest.mark.asyncio
async def test_reconnect_give_up_reports_on_error(monkeypatch):
    ws1 = FakeWs()
    connects = {"n": 0}

    async def fake_connect(api_url, token, system_prompt, tools):
        connects["n"] += 1
        if connects["n"] == 1:
            return ws1, "s1"
        raise ConnectionError("cloud down")

    monkeypatch.setattr(qb, "connect_qwen", fake_connect)
    _fast_scheduler(monkeypatch, delay=0.002, max_attempts=3)

    errors: list[str] = []

    async def on_error(message: str) -> None:
        errors.append(message)

    bridge = QwenRealtimeBridge(on_audio_out=_noop, on_error=on_error, api_url="ws://fake")
    await bridge.start()
    ws1.recv_error = ConnectionResetError("received 1007 (abnormal closure)")

    for _ in range(400):
        if errors and bridge.dead:
            break
        await asyncio.sleep(0.005)
    assert bridge.dead is True, "连续 3 次重连失败后应进入 dead 终态"
    assert errors, "放弃后必须经 on_error 上报（旧实现永久静默）"
    assert "重连失败" in errors[0], "上报消息应说明重连放弃"
    assert "cloud down" in errors[0], "上报消息应含最终异常信息"

    # dead 后 feed 不抛异常（20ms 高频调用方安全）
    await bridge.feed_pcm(b"\x00\x00" * 32)
    await bridge.close()


# ---------- 4. session.py 接线：on_error → cloud_engine_down + ctrl ----------

@pytest.mark.asyncio
async def test_session_logs_cloud_engine_down_on_qwen_error(caplog):
    ctrl: list[dict] = []

    async def send_msg(msg: dict) -> None:
        ctrl.append(msg)

    s = PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=send_msg,
        apm_api_url="ws://fake", apm_system_prompt="p",
        voice_engine="qwen",
    )
    assert s.apm._on_error is not None, "qwen 桥必须接线 on_error 回调"

    with caplog.at_level(logging.WARNING, logger="rtc_bridge.session"):
        await s.apm._on_error("云端引擎断开：code=response_idle_timeout received 1007")

    reasons = [r.getMessage() for r in caplog.records if "cloud_engine_down" in r.getMessage()]
    assert reasons, "接到 on_error 后应打结构化 WARNING cloud_engine_down"
    assert any("response_idle_timeout" in m for m in reasons)
    assert any(m.get("action") == "apm_error" for m in ctrl), "应同步 ctrl 上报手机端感知"


# ---------- 5. session.py `feed apm failed` 日志节流 ≥2s ----------

@pytest.mark.asyncio
async def test_feed_apm_failed_log_throttled(caplog):
    s = _make_session()
    await s.start()

    async def boom(_pcm: bytes) -> None:
        raise RuntimeError("received 1007 (abnormal closure)")

    s.feeder.feed = boom  # 绕过 EndDetect，直接模拟喂帧失败

    with caplog.at_level(logging.WARNING, logger="rtc_bridge.session"):
        for _ in range(30):              # 2s 内 30 帧
            await s.on_up_audio(b"\x00\x00" * 320)
        await asyncio.sleep(0.3)

    fails = [r for r in caplog.records if "feed apm failed" in r.getMessage()]
    assert len(fails) <= 2, f"30 帧失败只应至多 2 条节流日志（≥2s 一条），实际 {len(fails)} 条"
    assert len(fails) >= 1, "失败应仍有日志可观测"
