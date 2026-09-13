"""契约：`sim_phone.measure_speech_seconds` 必须与手机侧 phone.js **同口径**。

为什么需要（2026-09-13 测量脚手架污染实锤）
------------------------------------------
`tmp/measure-reply-rate.py` 旧判读拿「去静音的语音时长」比「edge-tts wav 总时长」：
口径不同 ⇒ 正常语速也必然算出 ≈0.57，脚本永远判「模型说得快」（实测 edge-tts 自身
43% 是静音帧）。修法是把**同口径**的语音时长测量抽到 `cloudbridge/sim_phone.py`，
供脚本与测试共用；规则必须与 `sidecar/phone.js:186-197`（SPEECH_RMS=phone.js:272）
逐行一致：16k mono s16，每 20ms（320 样本）一帧，帧内每 4 个样本取 1 求 RMS，
帧 RMS ≥ 60 记一个语音帧。

铁律：**依赖注入的替身覆盖率 ≠ 默认路径覆盖率**。本文件至少有一个用例真的把 wav
写到磁盘、再走 `measure_speech_seconds` 的默认路径读回来（不 mock 任何读取）。
"""
from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cloudbridge"))

import sim_phone  # noqa: E402

FRAME_SAMPLES = 320  # 20ms @16k mono s16


def _write_wav(path: Path, frames: list[list[int]]) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        for samples in frames:
            w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def _frame(amplitude: int, samples: int = FRAME_SAMPLES) -> list[int]:
    return [amplitude] * samples


def test_real_wav_default_path_counts_frames_exactly(tmp_path):
    """真实默认路径：写盘 → 读回，验证 (总时长, 语音时长) 精确到帧。"""
    frames = [
        _frame(0),      # 静音
        _frame(0),      # 静音
        _frame(1000),   # 语音
        _frame(500),    # 语音
        _frame(0),      # 静音
    ]
    path = _write_wav(tmp_path / "real.wav", frames)

    total, speech = sim_phone.measure_speech_seconds(path)

    assert total == pytest.approx(0.10), "5 帧 × 20ms —— 总时长同样按 20ms 量化"
    assert speech == pytest.approx(0.04), "幅值 1000 / 500 两帧为语音"


def test_all_silence_has_zero_speech(tmp_path):
    path = _write_wav(tmp_path / "sil.wav", [_frame(0) for _ in range(4)])
    total, speech = sim_phone.measure_speech_seconds(path)
    assert total == pytest.approx(0.08)
    assert speech == 0.0


def test_rms_threshold_60_counts_59_does_not(tmp_path):
    """门限与 phone.js 一致：RMS ≥ 60 记语音帧，59 不记（等幅帧 RMS == 幅值）。"""
    path = _write_wav(tmp_path / "edge.wav", [_frame(60), _frame(59)])
    total, speech = sim_phone.measure_speech_seconds(path)
    assert total == pytest.approx(0.04)
    assert speech == pytest.approx(0.02), "只有幅值 60 的那帧算语音"


def test_uses_every_4th_sample_like_phone_js(tmp_path):
    """phone.js 帧内**每 4 个样本取 1** 求 RMS。

    本用例把被抽样的样本（i%4==0）全置 0、其余置 3000：
      · 若实现忠实复刻 phone.js（只取每 4 个）→ RMS=0 → 该帧判静音；
      · 若实现改成全样本 RMS → RMS>0 → 该帧被误判成语音，本用例失败。
    """
    samples = [0] * FRAME_SAMPLES
    for i in range(FRAME_SAMPLES):
        if i % 4 != 0:
            samples[i] = 3000
    path = _write_wav(tmp_path / "sampling.wav", [samples])

    total, speech = sim_phone.measure_speech_seconds(path)

    assert total == pytest.approx(0.02)
    assert speech == 0.0, "被抽样的样本全为 0 → 必须判为静音（与 phone.js 同口径）"


def test_rejects_non_16k_mono_s16(tmp_path):
    """口径不同的 wav 直接报错，不做静默重采样——口径不同就不该出数。"""
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<4h", 1, 1, 1, 1))
    with pytest.raises(ValueError):
        sim_phone.measure_speech_seconds(stereo)

    rate44k = tmp_path / "44k.wav"
    with wave.open(str(rate44k), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(struct.pack("<2h", 1, 1))
    with pytest.raises(ValueError):
        sim_phone.measure_speech_seconds(rate44k)


def test_phone_js_rms_rule_is_the_source_of_truth():
    """守住口径常量：sim_phone.SPEECH_RMS 必须等于 sidecar/phone.js 里的 SPEECH_RMS。"""
    phone_js = (Path(__file__).resolve().parents[3] / "sidecar" / "phone.js").read_text(
        encoding="utf-8"
    )
    assert f"const SPEECH_RMS = {int(sim_phone.SPEECH_RMS)};" in phone_js, (
        "sim_phone.SPEECH_RMS 与 phone.js 不一致会导致两端口径漂移"
    )
