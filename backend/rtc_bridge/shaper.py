"""DownlinkShaper —— 下行整形器（PC-INTEGRATION §3.3 + SPEC §11.1 / AC-08~AC-10）

ApmBridge.on_audio_out 回调块大小随 API delta 变化（变长块）。
整形器：
1. PcmFrameBuffer 跨块保留 residue，只输出完整 640B 帧（16k s16 20ms）；
2. BoundedAudioQueue 有界存储（max_frames/max_bytes/最大帧龄，过载丢旧保新）；
3. 按「消费时长 = 帧长」节拍推送，避免一次性灌入导致手机端卡顿/爆音。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .bounded_audio_queue import BoundedAudioQueue
from .frame_buffer import PcmFrameBuffer

logger = logging.getLogger(__name__)

# 默认下行预算（AC-10；压力测试后可调）
DEFAULT_DOWN_MAX_FRAMES = 200
DEFAULT_DOWN_MAX_BYTES = 200 * 640
DEFAULT_DOWN_MAX_FRAME_AGE_MS = 1000


class DownlinkShaper:
    """变长块 → 定长 640B 帧 + 有界队列 + 节拍推送"""

    def __init__(
        self,
        send_frame: Callable[[bytes], Awaitable[None]],
        frame_ms: int = 20,
        sample_rate: int = 16000,
        *,
        max_frames: int = DEFAULT_DOWN_MAX_FRAMES,
        max_bytes: int = DEFAULT_DOWN_MAX_BYTES,
        max_frame_age_ms: int = DEFAULT_DOWN_MAX_FRAME_AGE_MS,
    ) -> None:
        self._send_frame = send_frame
        self._frame_bytes = int(sample_rate * 2 * (frame_ms / 1000))  # 20ms @16k mono s16 = 640B
        self._frame_s = frame_ms / 1000.0
        self._buffer = PcmFrameBuffer(frame_bytes=self._frame_bytes, tail_mode="drop")
        self._q = BoundedAudioQueue(
            max_frames=max_frames,
            max_bytes=max_bytes,
            max_frame_age_ms=max_frame_age_ms,
        )
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def push(self, pcm: bytes) -> None:
        """ApmBridge.on_audio_out 回调入口（非阻塞：跨块拆帧 + 有界入队）"""
        if self._closed:
            return
        for frame in self._buffer.feed(pcm):
            self._q.push(frame)
        self._wake.set()

    def flush_tail(self) -> None:
        """会话结束：不足帧显式处理（drop 模式记录 tail_dropped_bytes）"""
        for frame in self._buffer.flush():
            self._q.push(frame)
        self._wake.set()

    def reset(self) -> None:
        """丢弃已入队未推送的音频（远端重进/打断时清空防串话）"""
        self._q.flush()
        self._buffer.reset()
        self._wake.set()

    def metrics(self) -> dict:
        m = self._q.metrics()
        m["down_tail_dropped_bytes"] = self._buffer.tail_dropped_bytes
        m["down_total_frames"] = self._buffer.total_frames
        return m

    async def _run(self) -> None:
        while not self._closed:
            entry = self._q.pop()
            if entry is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            if self._closed:
                return
            try:
                await self._send_frame(entry.payload)
            except Exception as e:  # noqa: BLE001 - sidecar 断线不阻塞整形器
                logger.warning("shaper send frame failed: %s", e)
            await asyncio.sleep(self._frame_s)  # 节拍：20ms/帧

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.flush_tail()  # 会话结束尾帧显式处理并记录指标
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
