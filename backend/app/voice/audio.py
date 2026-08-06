"""音频工具（纯函数，无 IO/无副作用）：PCM 环形缓冲 + 格式转换"""
from __future__ import annotations

import array
from collections import deque

# 协议默认格式（mobile-voice-spec §4.3）
PCM_S16LE_16K = "pcm_s16le_16k"


class PcmRingBuffer:
    """PCM16 字节环形缓冲：上限 bytes 超限时丢弃最旧数据"""

    def __init__(self, max_bytes: int = 10 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._total = 0

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append(data)
        self._total += len(data)
        while self._total > self._max_bytes and self._chunks:
            dropped = self._chunks.popleft()
            self._total -= len(dropped)

    def drain(self) -> bytes:
        """取出全部缓冲并清空"""
        if not self._chunks:
            return b""
        out = b"".join(self._chunks)
        self._chunks.clear()
        self._total = 0
        return out

    def clear(self) -> None:
        self._chunks.clear()
        self._total = 0

    @property
    def size(self) -> int:
        return self._total

    @property
    def empty(self) -> bool:
        return self._total == 0


def pcm16_to_float32(pcm: bytes) -> array.array:
    """PCM16 LE 字节 → float32 样本（-1.0~1.0），供 sherpa-onnx 输入"""
    n = len(pcm) // 2
    samples = array.array("f")
    if n == 0:
        return samples
    raw = array.array("h")
    raw.frombytes(pcm[: n * 2])
    scale = 1.0 / 32768.0
    samples.fromlist([float(v) * scale for v in raw])
    return samples


def float32_to_pcm16(samples) -> bytes:
    """float32 样本 → PCM16 LE 字节（下行还原，供测试/调试）"""
    raw = array.array("h", [max(-32768, min(32767, int(v * 32767))) for v in samples])
    return raw.tobytes()


def is_valid_pcm16(pcm: bytes) -> bool:
    """粗校验：PCM16 字节数必须为偶数且非空"""
    return len(pcm) > 0 and len(pcm) % 2 == 0
