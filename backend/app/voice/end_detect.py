"""停顿补静音（说完判定）—— RTC 与旧 WS 路径共用（PC-INTEGRATION §3.4）

背景：手机 TRTC SDK / WS 采集会持续送帧（含底噪），模型 VAD 判定不了"你说完"→ 永不回复
（2026-08-06 现场实锤）。本模块在低能量持续 >silence_s（默认 1.2s）时向云端补
pad_s（默认 2s）纯静音，一次性触发模型说完判定；用户再开口（能量回升）→ 重置状态，
正常 feed（全双工 barge-in 不受影响）。

用法（rtc_bridge / 旧 WS 网关共用）：
    feeder = EndDetectFeeder(feed=apm_bridge.feed_pcm)
    await feeder.feed(pcm_16k_s16)
    feeder.reset()   # 远端用户重进 / 新会话时重置，防跨会话状态污染
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

# 16k s16 PCM 平均能量阈值（说话通常 >500；静音底噪 <200）
SILENCE_RMS_THRESHOLD = 400.0


def pcm_rms(s16: bytes) -> float:
    """16bit s16 PCM 平均能量"""
    if not s16:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(s16[: len(s16) // 2 * 2])
    return sum(abs(x) for x in samples) / len(samples) if samples else 0.0


class EndDetectFeeder:
    """停顿补静音包装：包住 ApmBridge.feed_pcm，注入说完判定语义"""

    def __init__(
        self,
        feed: Callable[[bytes], Awaitable[None]],
        silence_s: float = 1.2,
        pad_s: float = 2.0,
        sample_rate: int = 16000,
    ) -> None:
        self._feed = feed
        self._silence_s = silence_s
        self._pad_s = pad_s
        self._sample_rate = sample_rate
        self._last_voice_ts = time.time()
        self._silence_padded = False

    async def feed(self, s16: bytes) -> None:
        """喂一帧 16k s16：有声音正常 feed；静音超过阈值补静音触发说完"""
        now = time.time()
        if pcm_rms(s16) > SILENCE_RMS_THRESHOLD:
            # 有声音：更新最后语音时间，清补静音标记
            self._last_voice_ts = now
            self._silence_padded = False
            await self._feed(s16)
            return
        # 静音帧：若已停顿 >silence_s 且未补过 → 补静音（说完标记）
        if not self._silence_padded and (now - self._last_voice_ts) > self._silence_s:
            self._silence_padded = True
            await self._feed(b"\x00\x00" * self._sample_rate * int(self._pad_s))
        else:
            await self._feed(s16)

    def reset(self) -> None:
        """重置说完判定状态（远端用户重进 / 新会话时调用，防跨会话状态污染）"""
        self._last_voice_ts = time.time()
        self._silence_padded = False
