"""P0-4：qwen tool_call 去同步化（不冻结 recv 循环）

根因（internal-latency-budget §1.2 C2 / P1-2）：收到 function_call 后在
_handle_event 里同步 await 本地工具（registry.handle_tool 同步执行在事件
循环里）再发二轮 response.create——工具执行期间 recv 循环停摆 = 下行音频
冻结，tool 时长全额计入首音频延迟（10s 首包第一嫌疑，假设 A）。

修复：function_call 入队 → 专职 worker 串行执行 → 完成后回调里发
conversation.item.create + response.create。并发保护选「单 worker 串行
队列」：实现最简、不丢任何调用、天然免锁（asyncio 单线程）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

import app.voice.qwen_realtime_bridge as qb
from app.voice.qwen_realtime_bridge import QwenRealtimeBridge


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

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
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


def _fast_scheduler(monkeypatch, delay: float = 0.005) -> None:
    real_init = qb.ReconnectScheduler.__init__

    def fast_init(self, attempt, on_give_up, on_retry=None, **_kwargs):
        real_init(self, attempt, on_give_up, on_retry, delay, delay, 1000)

    monkeypatch.setattr(qb.ReconnectScheduler, "__init__", fast_init)


async def _noop(_audio) -> None:
    return None


def _function_call_event(call_id: str, name: str = "agent_status") -> str:
    return json.dumps({
        "type": "response.function_call_arguments.done",
        "name": name,
        "arguments": json.dumps({"thread_id": "t1"}),
        "call_id": call_id,
    })


def _audio_delta_event() -> str:
    pcm24 = b"\x01\x00" * 240
    return json.dumps({
        "type": "response.audio.delta",
        "delta": base64.b64encode(pcm24).decode(),
    })


@pytest.mark.asyncio
async def test_tool_call_does_not_block_recv_loop(monkeypatch):
    """工具执行挂起期间，recv 循环必须继续处理事件（首个 audio.delta 不被延迟）"""
    ws1 = FakeWs()

    async def fake_connect(api_url, token, system_prompt, tools):
        return ws1, "s1"

    monkeypatch.setattr(qb, "connect_qwen", fake_connect)
    _fast_scheduler(monkeypatch)

    release = asyncio.Event()
    calls: list[str] = []
    audio_out: list[bytes] = []

    async def slow_tool(name: str, args: dict, call_id: str) -> str:
        calls.append(f"enter:{call_id}")
        try:
            await asyncio.wait_for(release.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        calls.append(f"exit:{call_id}")
        return "ok"

    bridge = QwenRealtimeBridge(on_audio_out=audio_out.append, on_tool_call=slow_tool,
                                api_url="ws://fake")
    await bridge.start()

    # 先到 function_call（工具会挂起），随后到 audio.delta
    ws1.incoming.put_nowait(_function_call_event("call-1"))
    await asyncio.sleep(0.05)          # 让 recv 循环拿到 function_call 并入队
    t0 = time.monotonic()
    ws1.incoming.put_nowait(_audio_delta_event())
    got_audio = False
    while time.monotonic() - t0 < 0.5:
        if audio_out:
            got_audio = True
            break
        await asyncio.sleep(0.01)
    audio_at = time.monotonic() - t0

    assert got_audio, "工具挂起期间 audio.delta 应照常下发（基线：recv 循环被冻结）"
    assert audio_at < 0.2, f"audio.delta 应立即处理，实际延迟 {audio_at:.3f}s"
    assert calls == ["enter:call-1"], "工具应已开始执行但未完成（挂起中）"

    # 释放工具 → 完成回调里发二轮 item.create + response.create
    release.set()
    for _ in range(100):
        sent_types = [m["type"] for m in ws1.sent]
        if "response.create" in sent_types:
            break
        await asyncio.sleep(0.01)
    sent_types = [m["type"] for m in ws1.sent]
    assert "conversation.item.create" in sent_types
    assert "response.create" in sent_types, "工具完成后应发二轮 response.create"
    out_item = next(m for m in ws1.sent if m["type"] == "conversation.item.create")
    assert out_item["item"]["call_id"] == "call-1"
    assert calls[-1] == "exit:call-1"
    await bridge.close()


@pytest.mark.asyncio
async def test_concurrent_tool_calls_serialized_without_loss(monkeypatch):
    """并发多个 function_call：串行执行不丢失、不交叠（单 worker 队列）"""
    ws1 = FakeWs()

    async def fake_connect(api_url, token, system_prompt, tools):
        return ws1, "s1"

    monkeypatch.setattr(qb, "connect_qwen", fake_connect)
    _fast_scheduler(monkeypatch)

    release = asyncio.Event()
    calls: list[str] = []

    async def slow_tool(name: str, args: dict, call_id: str) -> str:
        calls.append(f"enter:{call_id}")
        await asyncio.wait_for(release.wait(), timeout=2.0)
        calls.append(f"exit:{call_id}")
        return "ok"

    bridge = QwenRealtimeBridge(on_audio_out=_noop, on_tool_call=slow_tool,
                                api_url="ws://fake")
    await bridge.start()

    ws1.incoming.put_nowait(_function_call_event("call-1"))
    ws1.incoming.put_nowait(_function_call_event("call-2"))
    await asyncio.sleep(0.1)
    assert calls == ["enter:call-1"], "同一时间只允许一个工具在执行"

    release.set()
    for _ in range(200):
        if calls.count("exit") == 2:
            break
        await asyncio.sleep(0.01)
    # 串行：enter1 exit1 enter2 exit2（无交叠）；两个调用都完成不丢失
    assert calls == ["enter:call-1", "exit:call-1", "enter:call-2", "exit:call-2"], calls
    out_items = [m for m in ws1.sent if m["type"] == "conversation.item.create"]
    assert {m["item"]["call_id"] for m in out_items} == {"call-1", "call-2"}
    assert len([m for m in ws1.sent if m["type"] == "response.create"]) == 2
    await bridge.close()
