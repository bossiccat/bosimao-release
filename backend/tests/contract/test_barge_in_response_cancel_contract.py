"""契约：打断时必须**主动向模型发送 response.cancel**。

背景（2026-09-13 实测）
----------------------
`session.py` 原有注释假定「云端 smart_turn 会自己 response.cancel 并停发音频」，
但实测**否掉了该假设**：用户插话后旧 response 仍继续下发到自然结束（+3.18s）。
两条打断路径（本地能量 / 云端 VAD）都只清我们这一侧，从未告诉模型停下，
导致打断延迟恒在 1.5–1.8s。而音频一旦生成并交付给 TRTC SDK 就收不回来 ——
所以**必须从源头（模型）停**。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.voice.qwen_realtime_bridge import QwenRealtimeBridge  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


async def _noop(*_a, **_k) -> None:  # pragma: no cover - 回调占位
    return None


class _FakeWS:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(json.loads(data))


def _bridge() -> QwenRealtimeBridge:
    return QwenRealtimeBridge(on_audio_out=_noop)


def test_cancel_response_sends_protocol_event():
    b = _bridge()
    ws = _FakeWS()
    b._ws = ws                      # 协议契约测试：直接注入连接替身
    ok = asyncio.run(b.cancel_response())
    assert ok is True
    assert ws.sent == [{"type": "response.cancel"}]


def test_cancel_response_is_fail_soft_when_disconnected():
    b = _bridge()
    b._ws = None
    assert asyncio.run(b.cancel_response()) is False


def test_cancel_response_does_not_raise_on_transport_error():
    """打断路径上抛异常会污染音频回调 —— 必须 fail-soft。"""
    b = _bridge()
    b._ws = _FakeWS(fail=True)
    assert asyncio.run(b.cancel_response()) is False


def test_session_calls_cancel_on_both_barge_in_paths():
    """两条打断路径都必须调用取消（本地能量路径 + 云端 VAD 路径）。"""
    s = (ROOT / "backend" / "rtc_bridge" / "session.py").read_text(encoding="utf-8")
    assert "cancel_response" in s, "会话层必须调用引擎的取消能力"
    assert s.count("await self._cancel_model_response()") >= 2, \
        "本地与云端两条打断路径都要取消模型 response"
