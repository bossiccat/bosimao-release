"""契约：模型音频 24k→16k **必须抗混叠**。

背景（2026-09-12 音质根因）
--------------------------
`qwen_realtime_bridge.pcm24k_to_pcm16k` 原实现是朴素抽取（`idx*1.5` 直接取样，无低通）。
24k→16k 会把 8–12 kHz 折叠回 0–8 kHz 变成非谐波噪声。这条路径是**模型回复音频的入口**，
所以「问句（edge-tts 直接合成 16k）干净、回复发毛」—— 实测回复谱亮度比问句高 68%、音调正常，
正是高频噪声的指纹。

注意：**跨调用有状态**是设计的一部分（流式变长 delta 下，无状态滤波会在每块首尾引入瑕疵）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.voice.qwen_realtime_bridge import (  # noqa: E402
    _PCM16K_TAPS_ARR, pcm24k_to_pcm16k,
)


def _tone(freq: float, seconds: float, rate: int = 24000, amp: float = 0.6) -> bytes:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    return np.round(amp * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def _power(buf: bytes, freq: float, rate: int = 16000) -> float:
    x = np.frombuffer(buf, dtype=np.int16).astype(np.float64) / 32768.0
    if not len(x):
        return 0.0
    k = 2 * np.pi * freq / rate
    n = np.arange(len(x))
    c = np.cos(k * n) @ x
    s = np.sin(k * n) @ x
    return (c * c + s * s) / (len(x) ** 2)


def _naive(buf24k: bytes) -> bytes:
    """对照：原实现的朴素抽取。"""
    s = np.frombuffer(buf24k, dtype=np.int16)
    idx = (np.arange(len(s) * 2 // 3) * 1.5).astype(np.int64)
    return s[idx[idx < len(s)]].tobytes()


def test_filter_taps_are_normalised():
    assert abs(float(_PCM16K_TAPS_ARR.sum()) - 1.0) < 1e-9


def test_out_of_band_10k_is_suppressed():
    """10 kHz 在 16k 的奈奎斯特（8k）之上 → 折叠到 |10000-16000| = 6 kHz。必须被压掉。

    注意不能用 12 kHz：它在 24k 采样率下**正好是 Nyquist**，`sin(pi*n)` 恒为 0，
    测试会「空过」（这个陷阱就是被本文件的对照用例抓出来的）。
    """
    out = pcm24k_to_pcm16k(_tone(10000, 0.5))
    alias = _power(out, 6000)
    total = float(np.sqrt(np.mean(
        np.frombuffer(out, dtype=np.int16).astype(np.float64) ** 2))) / 32768.0
    assert alias < 1e-6, f"10kHz 折叠分量应被抑制，实测 {alias}"
    assert total < 0.02, f"带外信号应被大幅衰减，实测 rms {total}"


def test_naive_decimation_would_alias():
    """对照：朴素抽取会残留明显的 6 kHz 折叠分量 —— 证明本测试有区分力。"""
    assert _power(_naive(_tone(10000, 0.5)), 6000) > 1e-3


def test_in_band_1k_is_preserved():
    out = pcm24k_to_pcm16k(_tone(1000, 0.5))
    amp = float(np.sqrt(_power(out, 1000))) * 2
    assert abs(amp - 0.6) < 0.12, f"1kHz 幅度应≈0.6，实测 {amp:.3f}"


def test_stateful_across_calls_matches_whole_call():
    """跨调用有状态：分段转换与整段转换输出必须一致（否则块边界会有瑕疵）。"""
    raw = _tone(1000, 0.4)
    whole = pcm24k_to_pcm16k(raw)

    samples = np.frombuffer(raw, dtype=np.int16)
    chunk = 480  # 20ms @24k
    parts = []
    for i in range(0, len(samples), chunk):
        parts.append(pcm24k_to_pcm16k(samples[i:i + chunk].tobytes()))
    piecewise = b"".join(parts)

    a = np.frombuffer(whole, dtype=np.int16).astype(np.int64)
    b = np.frombuffer(piecewise, dtype=np.int16).astype(np.int64)
    n = min(len(a), len(b))
    assert n > 0
    assert int(np.max(np.abs(a[:n] - b[:n]))) <= 2, "分段与整段输出应几乎一致"
