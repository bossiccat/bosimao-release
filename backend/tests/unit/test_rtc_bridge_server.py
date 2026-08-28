"""rtc_bridge server/session/shaper 单测（mock sidecar WS 客户端 + 假 ApmBridge，不触网）"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import pytest
import websockets

from rtc_bridge.config import BridgeConfig, load_bridge_config
from rtc_bridge.server import BridgeServer
from rtc_bridge.shaper import DownlinkShaper

# 关闭无关日志噪音
logging.disable(logging.WARNING)

SEC16K = 16000 * 2  # 1s @16k s16


class FakeApm:
    """假 ApmBridge：记录 feed_pcm；可主动触发 on_audio_out 下行回调"""

    def __init__(self, on_audio_out=None, on_text=None, on_state=None, **kwargs) -> None:
        self.on_audio_out = on_audio_out
        self.on_text = on_text
        self.on_state = on_state
        self.fed: list[bytes] = []
        self.closed = False
        self.started = False

    async def feed_pcm(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def close(self) -> None:
        self.closed = True

    async def start(self) -> None:
        self.started = True


@pytest.fixture
def fake_apm(monkeypatch):
    """把 backend.rtc_bridge.session.ApmBridge 换成 FakeApm"""
    import rtc_bridge.session as session_mod

    instances: list[FakeApm] = []

    def factory(*args, **kwargs):
        inst = FakeApm(*args, **kwargs)
        instances.append(inst)
        return inst

    monkeypatch.setattr(session_mod, "ApmBridge", factory)
    return instances


class FakeRedemption:
    async def redeem(self, hello: dict) -> dict:
        return {
            "redeemed": True,
            **{key: hello[key] for key in (
                "session_id", "device_id", "room_id", "sidecar_user_id", "generation"
            )},
            "expires_at": "2026-08-25T10:00:00Z",
        }


class RejectingRedemption:
    async def redeem(self, hello: dict) -> dict:
        raise RuntimeError("redemption rejected")


class BarrierRedemption(FakeRedemption):
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.calls = 0

    async def redeem(self, hello: dict) -> dict:
        self.calls += 1
        if self.calls == 2:
            self.ready.set()
        await self.ready.wait()
        return await super().redeem(hello)


def _hello(session_id: str, device_id: str, room_id: str) -> dict:
    return {
        "type": "hello", "proof": f"proof-{session_id}",
        "nonce": f"nonce-{session_id}-current", "jti": f"jti-{session_id}",
        "session_id": session_id, "device_id": device_id, "room_id": room_id,
        "sidecar_user_id": "jax-pc-sidecar", "generation": 0,
        "protocol_version": "1.0",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
            "frame_ms": 20, "frame_bytes": 640,
        },
    }


async def _start_server():
    cfg = BridgeConfig(ws_port=0, voice_engine="apm")  # 端口 0 → 系统分配
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    bridge = BridgeServer(cfg, state, redemption=FakeRedemption())
    # 手动起 websockets 服务（端口 0 由 websockets 分配）
    server = await websockets.serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return bridge, state, server, port


class FakeWorkerRunner:
    def __init__(self, registry) -> None:
        self.registry = registry
        self.started: list[str] = []
        self.cancelled: list[str] = []

    async def start(self, thread_id: str) -> dict:
        self.started.append(thread_id)
        await asyncio.sleep(0)
        return self.registry.update(thread_id, "completed", "后台 Worker 已完成测试只读任务。")

    async def cancel(self, thread_id: str) -> bool:
        self.cancelled.append(thread_id)
        self.registry.update(thread_id, "cancelled", "后台 Worker 已取消。")
        return True


@pytest.mark.asyncio
async def test_spawn_agent_thread_schedules_worker_and_returns_persistent_thread(tmp_path):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    from app.brain.agent_thread_registry import AgentThreadRegistry

    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    worker = FakeWorkerRunner(registry)
    bridge = BridgeServer(cfg, state, thread_registry=registry, worker_runner=worker)

    raw = await bridge._handle_agent_tool(
        "spawn_agent_thread", {"user_speech": "只读检查"}, "call-1"
    )
    initial = json.loads(raw)
    restored = registry.get(initial["thread_id"])
    assert initial["status"] == "awaiting_approval"
    assert initial["thread_id"] not in worker.started
    assert restored is not None
    assert restored["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_cancel_agent_thread_cancels_owned_worker(tmp_path):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    from app.brain.agent_thread_registry import AgentThreadRegistry

    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    worker = FakeWorkerRunner(registry)
    bridge = BridgeServer(cfg, state, thread_registry=registry, worker_runner=worker)
    thread = registry.spawn("停止测试")

    raw = await bridge._handle_agent_tool(
        "steer_agent_thread", {"thread_id": thread["thread_id"], "action": "cancel"}, "call-2"
    )
    result = json.loads(raw)

    assert worker.cancelled == [thread["thread_id"]]
    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_approval_release_starts_only_matching_waiting_worker(tmp_path):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    from app.brain.agent_thread_registry import AgentThreadRegistry

    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    worker = FakeWorkerRunner(registry)
    bridge = BridgeServer(cfg, state, thread_registry=registry, worker_runner=worker)
    thread = registry.spawn("需审批任务")
    pending = registry.handle_tool(
        "approve_reply",
        {"thread_id": thread["thread_id"], "summary": "确认后执行"},
    )

    wrong = await bridge.approve_agent_thread(thread["thread_id"], "wrong")
    assert wrong["error"] == "approval_mismatch"
    assert worker.started == []

    released = await bridge.approve_agent_thread(thread["thread_id"], pending["approval_id"])
    await bridge.wait_for_worker(released["thread_id"])
    assert released["status"] == "queued"
    assert worker.started == [thread["thread_id"]]
    assert registry.get(thread["thread_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_bridge_consumes_approval_from_independent_registry_and_restarts(tmp_path):
    cfg = BridgeConfig(ws_port=0)
    state = {"sidecar_connected": False, "room_id": "", "device_id": "", "_session_ref": None}
    from app.brain.agent_thread_registry import AgentThreadRegistry

    db = str(tmp_path / "threads.sqlite3")
    api_registry = AgentThreadRegistry(db)
    bridge_registry = AgentThreadRegistry(db)
    worker = FakeWorkerRunner(bridge_registry)
    thread = api_registry.spawn("跨进程审批任务")
    pending = api_registry.handle_tool("approve_reply", {
        "thread_id": thread["thread_id"], "summary": "确认后执行",
    })
    api_registry.approve(thread["thread_id"], pending["approval_id"])

    bridge = BridgeServer(cfg, state, thread_registry=bridge_registry, worker_runner=worker)
    await bridge.start_command_consumer(interval=0.01)
    await asyncio.sleep(0.05)
    await bridge.wait_for_worker(thread["thread_id"])
    await bridge.stop_command_consumer()

    assert worker.started == [thread["thread_id"]]
    assert bridge_registry.claim_commands() == []


@pytest.mark.asyncio
async def test_hello_and_ready(fake_apm):
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-001", "dev-001", "jax-dev-001")))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["type"] == "ready"
            assert state["sidecar_connected"] is True
            assert state["room_id"] == "jax-dev-001"
            assert fake_apm[0].fed == []  # 懒初始化：hello 不建 APM 会话
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_up_audio_feeds_apm(fake_apm):
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-001", "dev-001", "jax-dev-001")))
            await ws.recv()  # ready
            pcm = b"\x12\x34" * 3200
            await ws.send(json.dumps({"type": "up_audio", "pcm_b64": base64.b64encode(pcm).decode()}))
            await asyncio.sleep(0.2)
            assert fake_apm[0].fed and fake_apm[0].fed[0] == pcm
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_down_audio_roundtrip(fake_apm):
    """假 APM 触发 on_audio_out → sidecar 应收到 down_audio"""
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-001", "dev-001", "jax-dev-001")))
            await ws.recv()  # ready
            reply = b"\xab\xcd" * 320  # 640B = 20ms @16k（一个整形帧）
            await fake_apm[0].on_audio_out(reply)
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["type"] == "down_audio"
            assert base64.b64decode(msg["pcm_b64"]) == reply
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_peer_enter_leave_lifecycle(fake_apm):
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-001", "dev-001", "jax-dev-001")))
            await ws.recv()  # ready
            await ws.send(json.dumps({"type": "peer_state", "state": "enter", "user_id": "dev-001"}))
            await asyncio.sleep(0.1)
            assert fake_apm[0].closed is False
            await ws.send(json.dumps({"type": "peer_state", "state": "leave", "user_id": "dev-001"}))
            await asyncio.sleep(0.2)
            assert fake_apm[0].closed is True  # 远端离开 → 释放 APM 会话
    finally:
        server.close()
        await server.wait_closed()


def test_load_bridge_config_defaults_and_env(monkeypatch):
    cfg = load_bridge_config({})
    assert cfg.ws_port == 19092
    assert cfg.health_port == 19093
    assert cfg.test_audio_enabled is False
    cfg = load_bridge_config({
        "RTC_BRIDGE_WS_PORT": "19999",
        "RTC_BRIDGE_HEALTH_PORT": "19998",
        "RTC_BRIDGE_TEST_AUDIO_ENABLED": "true",
    })
    assert cfg.ws_port == 19999
    assert cfg.health_port == 19998
    assert cfg.test_audio_enabled is True


@pytest.mark.asyncio
async def test_health_test_audio_is_not_registered_by_default():
    """Production/default health surface must not expose the audio injection hook."""
    from rtc_bridge.health import HealthServer

    called = False

    async def inject() -> bool:
        nonlocal called
        called = True
        return True

    health = HealthServer("127.0.0.1", 0, {}, on_test_audio=inject)
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /test_audio HTTP/1.1\r\nHost: localhost\r\n\r\n")
    reader.feed_eof()

    class Writer:
        def __init__(self) -> None:
            self.data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    writer = Writer()
    await health._handle(reader, writer)
    assert b"404 Not Found" in writer.data
    assert called is False


@pytest.mark.asyncio
async def test_health_test_audio_requires_explicit_test_mode():
    from rtc_bridge.health import HealthServer

    called = False

    async def inject() -> bool:
        nonlocal called
        called = True
        return True

    health = HealthServer(
        "127.0.0.1", 0, {}, on_test_audio=inject, test_audio_enabled=True
    )
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /test_audio HTTP/1.1\r\nHost: localhost\r\n\r\n")
    reader.feed_eof()

    class Writer:
        def __init__(self) -> None:
            self.data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    writer = Writer()
    await health._handle(reader, writer)
    assert b"200 OK" in writer.data
    assert called is True


@pytest.mark.asyncio
async def test_health_metrics_serializable(fake_apm):
    """health /metrics 必须可 json 序列化（不泄露 _session_ref 对象）"""
    from rtc_bridge.health import HealthServer

    bridge, state, server, port = await _start_server()
    try:
        health = HealthServer("127.0.0.1", 0, state)
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-001", "dev-001", "jax-dev-001")))
            await ws.recv()  # ready
            # 连接中（有 _session_ref 指向 PeerVoiceSession）→ metrics 仍可序列化
            m = health._metrics()
            assert json.dumps(m) is not None
            assert "up_frames" in m
            assert "_session_ref" not in m
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_terminate_device_closes_existing_bridge_session(fake_apm):
    bridge, state, server, port = await _start_server()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps(_hello("session-revoke", "dev-revoke", "room-revoke")))
            await ws.recv()
            assert await bridge.terminate_device("dev-revoke", ["session-revoke"]) == ["session-revoke"]
            closing = json.loads(await ws.recv())
            assert closing == {
                "type": "ctrl", "action": "exit", "reason": "device_revoked"
            }
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()
        assert state["sidecar_connected"] is False
        assert state["room_id"] == ""
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_failed_candidate_keeps_active_session_and_replies_on_candidate(fake_apm):
    bridge, state, server, port = await _start_server()
    url = f"ws://127.0.0.1:{port}"
    try:
        async with websockets.connect(url) as ws_a:
            await ws_a.send(json.dumps(_hello("session-a", "dev-a", "r-a")))
            assert json.loads(await ws_a.recv())["type"] == "ready"
            active_ws, active_session = bridge._ws, bridge._session
            bridge._redemption = RejectingRedemption()
            async with websockets.connect(url) as ws_b:
                malformed = _hello("session-b", "dev-b", "r-b")
                malformed["proof"] = ""
                await ws_b.send(json.dumps(malformed))
                failure = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=5))
                assert failure == {"type": "ctrl", "action": "exit", "reason": "invalid_session_hello"}
                assert bridge._ws is active_ws
                assert bridge._session is active_session
                assert state["sidecar_connected"] is True
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws_a.recv(), timeout=0.2)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_redeem_rejection_keeps_active_session_and_replies_candidate(fake_apm):
    bridge, state, server, port = await _start_server()
    bridge._redemption = RejectingRedemption()
    url = f"ws://127.0.0.1:{port}"
    try:
        async with websockets.connect(url) as ws_a:
            bridge._redemption = FakeRedemption()
            await ws_a.send(json.dumps(_hello("session-a", "dev-a", "r-a")))
            assert json.loads(await ws_a.recv())["type"] == "ready"
            active_ws, active_session = bridge._ws, bridge._session
            bridge._redemption = RejectingRedemption()
            async with websockets.connect(url) as ws_b:
                await ws_b.send(json.dumps(_hello("session-b", "dev-b", "r-b")))
                failure = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=5))
                assert failure == {"type": "ctrl", "action": "exit", "reason": "hello_redemption_failed"}
                assert bridge._ws is active_ws
                assert bridge._session is active_session
                assert state["sidecar_connected"] is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_concurrent_candidates_activate_in_completion_order(fake_apm):
    bridge, state, server, port = await _start_server()
    redemption = BarrierRedemption()
    bridge._redemption = redemption
    url = f"ws://127.0.0.1:{port}"
    try:
        async with websockets.connect(url) as ws_a, websockets.connect(url) as ws_b:
            await ws_a.send(json.dumps(_hello("session-a", "dev-a", "r-a")))
            await ws_b.send(json.dumps(_hello("session-b", "dev-b", "r-b")))
            await asyncio.wait_for(redemption.ready.wait(), timeout=5)
            replies = await asyncio.gather(ws_a.recv(), ws_b.recv())
            assert {json.loads(reply)["type"] for reply in replies} == {"ready"}
            assert bridge._session_id in {"session-a", "session-b"}
            assert bridge._ws is not None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_replace_semantics_old_cleanup_does_not_kill_new(fake_apm):
    """P1 回归：旧连接被顶替后的 cleanup 不得误伤新连接（压测 S6 实锤的顶替竞态）。
    A 被 B 顶替 → A 的 finally 清理应跳过（身份检查）→ B 的 session 存活、可继续收流。"""
    bridge, state, server, port = await _start_server()
    url = f"ws://127.0.0.1:{port}"

    a_events: list[str] = []

    async def conn_a():
        try:
            async with websockets.connect(url, open_timeout=5) as ws:
                await ws.send(json.dumps(_hello("session-a", "dev-a", "r-a")))
                await asyncio.wait_for(ws.recv(), timeout=5)  # ready
                try:
                    await asyncio.wait_for(ws.recv(), timeout=8)
                except websockets.exceptions.ConnectionClosed:
                    a_events.append("closed")
                except asyncio.TimeoutError:
                    a_events.append("timeout")
        except Exception as e:  # noqa: BLE001
            a_events.append(f"err:{str(e)[:40]}")

    task_a = asyncio.create_task(conn_a())
    await asyncio.sleep(0.5)

    # B 连接（顶替 A）
    async with websockets.connect(url, open_timeout=5) as ws_b:
        await ws_b.send(json.dumps(_hello("session-b", "dev-b", "r-b")))
        ready = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=5))
        assert ready["type"] == "ready"

        # 等 A 的 finally 清理执行完（A 已收到 close）
        await asyncio.sleep(0.8)
        assert "closed" in a_events, f"A 应被顶替关闭，实际 {a_events}"

        # 关键断言：B 的 session 必须存活（未被 A 的 cleanup 误关）
        assert state["sidecar_connected"] is True, "B 连接应保持 sidecar_connected=True"
        assert bridge._session is not None and bridge._session.device_id == "dev-b"

        # B 发音频 → 必须进入 B 的 session（fake_apm.fed 增长）
        pcm = bytes(640)  # 20ms @16k
        before = len(fake_apm[-1].fed) if fake_apm else 0
        await ws_b.send(json.dumps({"type": "up_audio", "pcm_b64": base64.b64encode(pcm).decode()}))
        await asyncio.sleep(0.3)
        assert len(fake_apm[-1].fed) > before, "B 的 up_audio 必须被 B 的 session 接收"

    await task_a
    server.close()
    await server.wait_closed()


# ---------- Task 5：shaper 跨块 residue 与有界背压（AC-08/AC-09/AC-10） ----------

@pytest.mark.asyncio
async def test_shaper_outputs_only_full_640_frames():
    """变长块跨块凑帧：只发送完整 640B 帧；尾帧 drop 模式显式记录指标"""
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    shaper = DownlinkShaper(send, frame_ms=20, sample_rate=16000)
    shaper.start()
    await shaper.push(b"a" * 300)
    await shaper.push(b"b" * 340)          # 300+340 = 640 → 完整帧
    await asyncio.sleep(0.06)
    assert len(sent) == 1
    assert sent[0] == b"a" * 300 + b"b" * 340
    await shaper.push(b"c" * 100)          # 不足帧 residue
    await shaper.stop()                    # flush_tail：drop 模式
    assert all(len(frame) == 640 for frame in sent)
    assert shaper.metrics()["down_tail_dropped_bytes"] == 100


@pytest.mark.asyncio
async def test_shaper_backpressure_drops_oldest():
    """小预算队列过载：丢旧保新，drops/backpressure/high_watermark 指标可观测"""
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    shaper = DownlinkShaper(send, frame_ms=20, sample_rate=16000,
                            max_frames=2, max_bytes=2 * 640, max_frame_age_ms=10000)
    shaper.start()
    for i in range(5):
        await shaper.push(bytes([i]) * 640)
    await asyncio.sleep(0.15)
    await shaper.stop()
    metrics = shaper.metrics()
    assert metrics["queue_drops"] >= 2
    assert metrics["backpressure_events"] >= 2
    assert metrics["queue_high_watermark"] <= 2
    assert all(len(frame) == 640 for frame in sent)
