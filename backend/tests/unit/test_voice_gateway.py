"""voice 网关 WS 协议单测（mobile-voice-spec §7/§8，M1）

覆盖：二进制帧编解码 / hello 鉴权 / 音频累积→半双工处理→下行 /
互斥踢连接 / 心跳 / cancel / stt 不可用 / 状态端点。STT/TTS 全 mock。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.voice.config import VoiceConfig, VoiceHalfDuplexConfig, VoiceSessionConfig
from app.voice.e2ee import build_e2ee
from app.voice.half_duplex import HalfDuplex
from app.voice.schemas import (
    AUDIO_FRAME_HEADER_LEN,
    decode_audio_frame,
    encode_audio_frame,
)
from app.voice.session import VoiceSessionManager
from app.voice.stt_sherpa import SttSherpa
from app.voice.tts_edge import TtsEdge, TtsResult
from app.api.routes_voice import create_voice_router
from relay.relay_protocol import RelayE2EE, derive_key_from_passphrase, DEFAULT_E2EE_PASSPHRASE

PCM_BYTES = b"\x00\x00" * 800  # 1600B = 800 个 16-bit 样本（~50ms @16k）


class FakeStt:
    """mock STT：记录收到的 PCM，返回固定文本"""

    def __init__(self, text: str = "帮我重构数据层") -> None:
        self.text = text
        self.received_pcm: list[bytes] = []

    def transcribe(self, pcm: bytes):
        self.received_pcm.append(pcm)
        return _SttRes(self.text, "ok")

    def model_status(self) -> str:
        return "ok"

    def available(self) -> bool:
        return True


class _SttRes:
    def __init__(self, text: str, status: str) -> None:
        self.text = text
        self.model_status = status


class FakeTts:
    """mock TTS：返回固定音频字节"""

    def __init__(self, audio: bytes = b"\xff\xfb" * 64) -> None:
        self.audio = audio
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> TtsResult:
        self.calls.append(text)
        return TtsResult(data=self.audio, voice="zh-CN-XiaoxiaoNeural")


def make_cfg(**kw) -> VoiceConfig:
    defaults = VoiceConfig(
        half_duplex=VoiceHalfDuplexConfig(stt_model_dir="/nonexistent/models"),
        session=VoiceSessionConfig(heartbeat_interval_s=0.2, heartbeat_timeout_s=30),
    )
    return defaults.model_copy(update=kw)


def make_app(stt: FakeStt | None = None, tts: FakeTts | None = None,
             cfg: VoiceConfig | None = None) -> tuple[TestClient, FakeStt, FakeTts]:
    cfg = cfg or make_cfg()
    stt = stt or FakeStt()
    tts = tts or FakeTts()
    engine = HalfDuplex(stt=stt, tts=tts, trigger_words=cfg.half_duplex.brain_trigger)
    manager = VoiceSessionManager(cfg, engine)
    app = FastAPI()
    app.include_router(create_voice_router(cfg, manager))
    return TestClient(app), stt, tts


def _hello(device_id: str = "test-phone", **extra) -> str:
    msg = {"type": "hello", "role": "phone", "device_id": device_id, "app_version": "0.1.0"}
    msg.update(extra)
    return json.dumps(msg, ensure_ascii=False)


def _audio_frame(seq: int, payload: bytes, ts: int = 1000) -> bytes:
    return encode_audio_frame(seq, ts, payload)


def _recv_until(ws, target_type: str, max_frames: int = 30) -> list[dict]:
    """接收帧直到出现目标 type（测试辅助；starlette receive 无 timeout，靠帧数兜底）"""
    frames: list[dict] = []
    for _ in range(max_frames):
        frame = ws.receive()
        if frame.get("text"):
            try:
                msg = json.loads(frame["text"])
            except json.JSONDecodeError:
                msg = {"raw": frame["text"]}
            frames.append(msg)
            if msg.get("type") == target_type:
                return frames
        elif frame.get("bytes"):
            frames.append({"binary": frame["bytes"]})
    return frames


# ---------- 帧协议 ----------
def test_audio_frame_roundtrip():
    data = encode_audio_frame(7, 123456789, b"pcm-payload")
    assert data[0] == 0x02
    assert len(data) == AUDIO_FRAME_HEADER_LEN + len(b"pcm-payload")
    chunk = decode_audio_frame(data)
    assert chunk.seq == 7
    assert chunk.ts_ms == 123456789
    assert chunk.payload == b"pcm-payload"


def test_audio_frame_decode_errors():
    with pytest.raises(ValueError):
        decode_audio_frame(b"\x02\x00")  # 过短
    with pytest.raises(ValueError):
        decode_audio_frame(b"\x99" + b"\x00" * 20)  # magic 错误


# ---------- hello 鉴权 ----------
def test_hello_ready_and_auth_reject():
    cfg = make_cfg(token="secret", require_token=True)
    client, _, _ = make_app(cfg=cfg)

    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello(device_id="d1", token="wrong"))
        err = json.loads(ws.receive_text())
        assert err["type"] == "error" and err["code"] == "auth_failed"

    with client.websocket_connect("/ws/voice?token=secret") as ws:
        ws.send_text(_hello(device_id="d1"))
        ready = json.loads(ws.receive_text())
        assert ready["type"] == "ready"
        assert ready["audio"]["up"] == "pcm_s16le_16k"


def test_hello_must_be_first_frame():
    client, _, _ = make_app()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "wake"}))
        err = json.loads(ws.receive_text())
        assert err["code"] == "bad_frame"


# ---------- 音频累积 + 半双工处理 + 下行 ----------
def test_audio_accumulate_half_duplex_reply():
    client, stt, tts = make_app()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello())
        assert json.loads(ws.receive_text())["type"] == "ready"

        ws.send_text(json.dumps({"type": "audio_start", "ts": 1}))
        ws.send_bytes(_audio_frame(0, PCM_BYTES[:800]))
        ws.send_bytes(_audio_frame(1, PCM_BYTES[800:]))
        ws.send_text(json.dumps({"type": "audio_end", "ts": 2}))

        frames = _recv_until(ws, "reply_done")

    # STT 收到完整累积 PCM（两个 payload 拼接）
    assert stt.received_pcm, "STT 应被调用"
    assert stt.received_pcm[-1] == PCM_BYTES
    # TTS 收到回复文本
    assert tts.calls, "TTS 应被调用"

    types = [f["type"] for f in frames if "type" in f]
    assert "reply_done" in types
    assert any(f.get("type") == "session_state" and f.get("state") == "thinking" for f in frames)
    assert any(f.get("type") == "transcript" and f.get("is_final") is True for f in frames)
    # 下行音频二进制帧存在且带 0x02 头
    down_bin = [f["binary"] for f in frames if "binary" in f]
    assert down_bin and down_bin[0][0] == 0x02
    chunk = decode_audio_frame(down_bin[0])
    assert chunk.payload == tts.audio


# ---------- 互斥踢连接 ----------
def test_single_device_mutex_kick():
    client, _, _ = make_app()
    with client.websocket_connect("/ws/voice") as ws_a:
        ws_a.send_text(_hello(device_id="dup"))
        assert json.loads(ws_a.receive_text())["type"] == "ready"

        with client.websocket_connect("/ws/voice") as ws_b:
            ws_b.send_text(_hello(device_id="dup"))
            assert json.loads(ws_b.receive_text())["type"] == "ready"
            # 旧连接被踢：收到 kicked 错误帧
            err = json.loads(ws_a.receive_text())
            assert err["type"] == "error" and err["code"] == "kicked"


# ---------- 心跳 ----------
def test_heartbeat_pong():
    client, _, _ = make_app()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello())
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "heartbeat", "ts": 42}))
        pong = json.loads(ws.receive_text())
        assert pong["type"] == "pong"


def test_server_ping():
    client, _, _ = make_app()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello())
        assert json.loads(ws.receive_text())["type"] == "ready"
        # heartbeat_interval_s=0.2 → 服务端主动 ping（阻塞直到收到）
        ping = json.loads(ws.receive_text())
        assert ping["type"] == "ping"


def test_voice_heartbeat_config_sane():
    """heartbeat_timeout_s 必须 > heartbeat_interval_s：
    网关发 ping 后立即检查 last_rx，若 timeout ≤ interval，被动应答型客户端
    （relay_client 收到 ping 才回 heartbeat）每次检查必然超时被踢。
    2026-08-05 现场：voice.yaml 配 15s < 30s → jax.log 每 30s 一条 heartbeat timeout"""
    from app.voice.config import load_voice

    cfg = load_voice()
    assert cfg.session.heartbeat_timeout_s > cfg.session.heartbeat_interval_s, (
        f"heartbeat_timeout_s({cfg.session.heartbeat_timeout_s}) 必须大于 "
        f"heartbeat_interval_s({cfg.session.heartbeat_interval_s})"
    )


def test_heartbeat_timeout_closes_connection():
    """心跳超时后服务端必须主动 close WS（不能只 break 循环留下僵尸连接：
    客户端 TCP 仍 ESTABLISHED 无感知，网关也不再发帧——2026-08-05 现场僵死连接）"""
    import asyncio

    from app.voice.session import VoiceSession, _heartbeat_loop

    class FakeWs:
        def __init__(self) -> None:
            self.closed: list[tuple] = []

        async def send_text(self, s: str) -> None:
            pass

        async def close(self, code: int | None = None, reason: str | None = None) -> None:
            self.closed.append((code, reason))

    class FakeEngine:
        pass

    cfg = make_cfg(session=VoiceSessionConfig(heartbeat_interval_s=0.2, heartbeat_timeout_s=0.1))
    ws = FakeWs()
    session = VoiceSession(ws, "t-device", cfg, FakeEngine())  # type: ignore[arg-type]
    session.last_rx = time.time() - 5.0   # 已超时（距最后收帧 > timeout）

    async def run() -> None:
        task = asyncio.create_task(_heartbeat_loop(session, None))  # type: ignore[arg-type]
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert ws.closed, "心跳超时后服务端应主动 close WS 连接（而非留下僵尸连接）"
    assert ws.closed[0][0] == 1001


# ---------- cancel ----------
def test_cancel_clears_buffer():
    client, stt, _ = make_app()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello())
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "wake"}))
        ws.send_bytes(_audio_frame(0, PCM_BYTES))     # 先累积音频
        ws.send_text(json.dumps({"type": "cancel"}))   # 再取消 → 清空缓冲
        # wake → listening；cancel → monitoring（连收两帧）
        states = []
        for _ in range(4):
            msg = json.loads(ws.receive_text())
            if msg.get("type") == "session_state":
                states.append(msg.get("state"))
            if "listening" in states and "monitoring" in states:
                break
        assert "listening" in states and "monitoring" in states
        # 取消后 audio_end 触发处理，但缓冲已清空 → empty 错误，STT 不被调用
        ws.send_text(json.dumps({"type": "audio_end"}))
        frames = _recv_until(ws, "error", max_frames=10)
        assert any(f.get("type") == "error" and f.get("code") == "empty" for f in frames)
        assert stt.received_pcm == []


# ---------- STT 模型不可用 ----------
def test_stt_unavailable_error_frame():
    cfg = make_cfg()
    real_stt = SttSherpa("/nonexistent/models")
    engine = HalfDuplex(stt=real_stt, tts=TtsEdge())
    manager = VoiceSessionManager(cfg, engine)
    app = FastAPI()
    app.include_router(create_voice_router(cfg, manager))
    client = TestClient(app)

    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello())
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_bytes(_audio_frame(0, PCM_BYTES))
        ws.send_text(json.dumps({"type": "audio_end"}))
        frames = _recv_until(ws, "error", max_frames=10)
        assert any(f.get("type") == "error" and f.get("code") == "stt_unavailable" for f in frames)


# ---------- 未知控制帧 / 状态端点 ----------
def test_unknown_control_frame():
    client, _, _ = make_app()
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_hello())
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "bogus"}))
        err = json.loads(ws.receive_text())
        assert err["code"] == "bad_frame"


def test_voice_status_endpoint():
    client, _, _ = make_app()
    resp = client.get("/api/v1/voice/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["path"] == "B"
    assert body["data"]["engine"] == "half_duplex"


def test_voice_pair_endpoint():
    client, _, _ = make_app()
    resp = client.post("/api/v1/voice/pair")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["pairing_code"]) == 6
    assert data["token"]
