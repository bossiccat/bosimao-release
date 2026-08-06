"""DownlinkShaper —— 下行整形器（PC-INTEGRATION §3.3）

ApmBridge.on_audio_out 回调块大小随 API delta 变化（24k f32 → 16k s16 后仍为变长块）。
整形器把变长块按 sidecar 期望帧长（默认 20ms=640B @16k）拆帧，并按「消费时长
= len/32000 秒」的节拍推送，避免一次性灌入导致手机端卡顿/爆音（对齐官方示例 pacer 思路）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class DownlinkShaper:
    """变长块 → 定长帧 + 节拍推送"""

    def __init__(
        self,
        send_frame: Callable[[bytes], Awaitable[None]],
        frame_ms: int = 20,
        sample_rate: int = 16000,
    ) -> None:
        self._send_frame = send_frame
        self._frame_bytes = int(sample_rate * 2 * (frame_ms / 1000))  # 20ms @16k mono s16 = 640B
        self._frame_s = frame_ms / 1000.0
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._reset_event = asyncio.Event()
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def push(self, pcm: bytes) -> None:
        """ApmBridge.on_audio_out 回调入口（非阻塞入队）"""
        if self._closed:
            return
        self._q.put_nowait(pcm)

    def reset(self) -> None:
        """丢弃已入队未推送的尾部音频（可选延迟优化；远端用户重进时清空防串话）"""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _run(self) -> None:
        while not self._closed:
            block = await self._q.get()
            frames = self._split(block)
            for f in frames:
                if self._closed:
                    return
                try:
                    await self._send_frame(f)
                except Exception as e:  # noqa: BLE001 - sidecar 断线不阻塞整形器
                    logger.warning("shaper send frame failed: %s", e)
                await asyncio.sleep(self._frame_s)   # 节拍：20ms/帧

    def _split(self, block: bytes) -> list[bytes]:
        n = self._frame_bytes
        return [block[i : i + n] for i in range(0, len(block), n) if block[i : i + n]]

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
