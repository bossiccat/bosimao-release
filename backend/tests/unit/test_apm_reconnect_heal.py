"""A8：ApmBridge 断线自愈测试（mock ws，不触网）

覆盖审计实锤的失效路径：
1. 重连失败不再永久静默——退避调度循环持续重试
2. 连续失败达上限 → 放弃 → on_error 上报（手机端可感知）→ dead 终态
3. 重连成功 = 完整重握手（排队 + session.init）
4. 断线窗口音频帧丢弃并计数，不堆积
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.voice.apm_reconnect import ReconnectScheduler
from app.voice.apm_bridge import ApmBridge


# ---------- ReconnectScheduler 纯逻辑 ----------

def test_scheduler_backoff_sequence_and_cap():
    s = ReconnectScheduler(attempt=None, on_give_up=None)  # type: ignore[arg-type]
    assert [s.delay_for(i) for i in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert s.delay_for(20) == 60.0  # 封顶


@pytest.mark.asyncio
async def test_scheduler_gives_up_after_max_attempts():
    attempts: list[int] = []
    gave_up = []

    async def always_fail() -> bool:
        attempts.append(1)
        return False

    async def on_give_up() -> None:
        gave_up.append(True)

    s = ReconnectScheduler(attempt=always_fail, on_give_up=on_give_up,
                           base_delay_s=0.001, max_delay_s=0.001, max_attempts=3)
    assert s.schedule() is True
    await asyncio.sleep(0.05)
    assert len(attempts) == 3
    assert gave_up == [True]
    assert s.gave_up is True
    assert s.schedule() is False  # 放弃后不再接受调度


@pytest.mark.asyncio
async def test_scheduler_stops_on_success():
    calls = []

    async def fail_then_succeed() -> bool:
        calls.append(1)
        return len(calls) >= 2

    s = ReconnectScheduler(attempt=fail_then_succeed, on_give_up=None,  # type: ignore[arg-type]
                           base_delay_s=0.001, max_delay_s=0.001)
    s.schedule()
    await asyncio.sleep(0.05)
    assert len(calls) == 2
    assert s.failures == 0  # 成功清零
    assert not s.running


@pytest.mark.asyncio
async def test_scheduler_schedule_is_idempotent_while_running():
    calls: list[int] = []

    slow_started = asyncio.Event()

    async def attempt() -> bool:
        calls.append(1)
        if len(calls) == 1:
            await slow_started.wait()
        return True

    s = ReconnectScheduler(attempt=attempt, on_give_up=None,  # type: ignore[arg-type]
                           base_delay_s=0.001)
    assert s.schedule() is True
    await asyncio.sleep(0.01)  # 让循环进入第一次 attempt
    assert s.schedule() is False  # 已在跑 → 忽略，不开双循环
    slow_started.set()
    await asyncio.sleep(0.01)
    assert len(calls) == 1


# ---------- ApmBridge 断线自愈（mock ws） ----------

class FakeWs:
    """模拟 API ws：queue_done/session.created 握手 + 可控 send 抛错"""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.fail_send = False
        self.closed = False

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data) -> None:
        if self.fail_send:
            raise ConnectionError("send on closed")
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


def _primed_ws() -> FakeWs:
    ws = FakeWs()
    ws.incoming.put_nowait(json.dumps({"type": "session.queue_done"}))
    ws.incoming.put_nowait(json.dumps({"type": "session.created", "session_id": "s1"}))
    return ws


async def _noop(*a, **k):
    return None


@pytest.mark.asyncio
async def test_send_failure_triggers_backoff_retry_until_success(monkeypatch):
    """A8 核心：发送失败 → 退避重连循环 → 恢复后可继续上行（不再永久静默）"""
    import app.voice.apm_handshake as hs

    ws1 = FakeWs()
    ws1.fail_send = True   # 首连成功但发送即断
    ws2 = _primed_ws()     # 重连后的新链路
    connects = {"n": 0}

    async def fake_handshake(url, token, prompt):
        connects["n"] += 1
        return (ws1 if connects["n"] == 1 else ws2), f"s{connects['n']}"

    monkeypatch.setattr(hs, "connect_and_handshake", fake_handshake)
    # apm_bridge 已 import 的引用也要换
    import app.voice.apm_bridge as ab
    monkeypatch.setattr(ab, "connect_and_handshake", fake_handshake)
    # 压缩退避时间（先取原 init 再替换）
    _orig_init = ReconnectScheduler.__init__

    def fast_init(self, attempt, on_give_up, on_retry=None,
                  base_delay_s=0.01, max_delay_s=0.01, max_attempts=10):
        _orig_init(self, attempt, on_give_up, on_retry, 0.01, 0.01, 10)

    monkeypatch.setattr(ab.ReconnectScheduler, "__init__", fast_init)

    states: list[str] = []
    bridge = ApmBridge(on_audio_out=_noop, on_state=states.append, api_url="ws://fake")
    await bridge.feed_pcm(b"\x00\x00" * 16000)   # 首块 → 建连 + 发送失败
    assert bridge._ws is None                    # 链路标记断
    assert bridge._scheduler.running             # 退避循环已启动
    assert bridge.dropped_frames >= 1            # 断线窗口丢帧计数
    await bridge.feed_pcm(b"\x00\x00" * 8000)    # 断线窗口帧 → 丢弃计数
    assert bridge.dropped_frames >= 2
    # 等退避重连成功（最多 2s）
    for _ in range(400):
        if bridge._ws is ws2:
            break
        await asyncio.sleep(0.005)
    assert bridge._ws is ws2, "重连循环应重建链路（旧逻辑此处永久 None）"
    assert bridge.reconnects == 1
    assert "reconnected" in states
    # 恢复后上行畅通：喂满 1s → input.append 上行
    await bridge.feed_pcm(b"\x11\x22" * 16000)
    assert any(m.get("type") == "input.append" for m in ws2.sent)
    await bridge.close()


@pytest.mark.asyncio
async def test_reconnect_gives_up_reports_error_and_dead(monkeypatch):
    """A8：连续失败达上限 → on_error 上报 + dead 终态 + feed 丢弃不堆积"""
    import app.voice.apm_bridge as ab

    async def never(url, token, prompt):
        raise ConnectionError("cloud down")

    monkeypatch.setattr(ab, "connect_and_handshake", never)

    # 小步退避快进到放弃
    real_init = ReconnectScheduler.__init__

    def fast_init(self, attempt, on_give_up, on_retry=None,
                  base_delay_s=0.005, max_delay_s=0.005, max_attempts=3):
        real_init(self, attempt, on_give_up, on_retry, 0.005, 0.005, 3)

    monkeypatch.setattr(ab.ReconnectScheduler, "__init__", fast_init)

    errors: list[tuple[str, str]] = []

    async def on_error(code, message):
        errors.append((code, message))

    bridge = ApmBridge(on_audio_out=_noop, on_error=on_error, api_url="ws://fake")
    await bridge.feed_pcm(b"\x00\x00" * 16000)   # 首连失败 → 调度重连
    for _ in range(200):
        if bridge.dead:
            break
        await asyncio.sleep(0.005)
    assert bridge.dead is True
    assert errors and errors[0][0] == "apm_reconnect_gave_up"
    # dead 后 feed 丢弃计数、无异常、无内存堆积
    n = bridge.dropped_frames
    await bridge.feed_pcm(b"\x00\x00" * 16000)
    await bridge.feed_pcm(b"\x00\x00" * 16000)
    assert bridge.dropped_frames == n + 2
    assert len(bridge._up_buf) == 0
    await bridge.close()


@pytest.mark.asyncio
async def test_recv_loop_exit_schedules_reconnect(monkeypatch):
    """A8：recv 循环退出（服务端关会话）→ 自动重连（re-sync）"""
    import app.voice.apm_bridge as ab

    ws_instances = [_primed_ws(), _primed_ws()]
    connects = {"n": 0}

    async def fake_handshake(url, token, prompt):
        connects["n"] += 1
        return ws_instances[connects["n"] - 1], f"s{connects['n']}"

    monkeypatch.setattr(ab, "connect_and_handshake", fake_handshake)
    real_init = ReconnectScheduler.__init__

    def fast_init(self, attempt, on_give_up, on_retry=None,
                  base_delay_s=0.005, max_delay_s=0.005, max_attempts=10):
        real_init(self, attempt, on_give_up, on_retry, 0.005, 0.005, 10)

    monkeypatch.setattr(ab.ReconnectScheduler, "__init__", fast_init)

    bridge = ApmBridge(on_audio_out=_noop, api_url="ws://fake")
    await bridge.feed_pcm(b"\x00\x00" * 100)  # 懒建连（不满 1s 块，无发送）
    ws1 = bridge._ws
    assert ws1 is ws_instances[0]
    # 服务端主动关会话 → recv 循环退出
    ws1.incoming.put_nowait(json.dumps({"type": "session.closed", "reason": "server"}))
    for _ in range(400):
        if bridge._ws is ws_instances[1]:
            break
        await asyncio.sleep(0.005)
    assert bridge._ws is ws_instances[1], "session.closed 后应重连重握手"
    assert bridge.reconnects == 1
    await bridge.close()
