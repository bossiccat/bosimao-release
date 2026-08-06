"""sherpa-onnx STT 单测（模型未下载 → mock 路径，协议/工具全测）

真实模型不可用（CI/本机未下载）时验证：available=False / model_status=missing /
transcribe 抛 SttModelUnavailable；PCM 转换工具纯函数全绿。
"""
from __future__ import annotations

import pytest

from app.voice.audio import float32_to_pcm16, is_valid_pcm16, pcm16_to_float32
from app.voice.stt_sherpa import SttModelUnavailable, SttSherpa

MISSING_DIR = "/nonexistent/sherpa-models"


def test_model_unavailable_when_missing():
    stt = SttSherpa(MISSING_DIR)
    assert stt.available() is False
    assert stt.model_status() == "missing"


def test_transcribe_raises_when_missing():
    stt = SttSherpa(MISSING_DIR)
    with pytest.raises(SttModelUnavailable) as ei:
        stt.transcribe(b"\x00\x00" * 800)
    assert "download_sherpa_models" in str(ei.value)


def test_transcribe_empty_returns_empty():
    stt = SttSherpa(MISSING_DIR)
    res = stt.transcribe(b"")
    assert res.ok is False
    assert res.text == ""


# ---------- PCM 工具（纯函数） ----------
def test_pcm16_to_float32_bounds():
    # 0x0000 → 0.0；0x8000(-32768) → -1.0；0x7FFF(32767) → ≈1.0
    samples = pcm16_to_float32(b"\x00\x00" + b"\x00\x80" + b"\xff\x7f")
    assert len(samples) == 3
    assert samples[0] == 0.0
    assert samples[1] == -1.0
    assert abs(samples[2] - 1.0) < 1e-4


def test_pcm16_to_float32_roundtrip():
    pcm = b"\x00\x00" * 100
    samples = pcm16_to_float32(pcm)
    back = float32_to_pcm16(samples)
    assert back == pcm


def test_is_valid_pcm16():
    assert is_valid_pcm16(b"\x00\x00" * 10)
    assert not is_valid_pcm16(b"")
    assert not is_valid_pcm16(b"\x00\x00\x00")  # 奇数长度
