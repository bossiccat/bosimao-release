"""PcmDumpSink —— 下行/上行 PCM 取证落盘（JAX_DOWN_PCM_DUMP，P0 2026-09-06）

背景：埋点已证明 sidecar 出口 TTFB 正常、丢弃极少，但「PC 侧下行音频内容
本身是否干净（有无空隙/静音洞/采样错乱）」缺直接证据，TRTC→手机最后一跳
无法归因。人耳/工具复核需要的不是计数器，是原始 PCM。

设计约束：
- 不拖慢音频路径：audio 线程只做 bytearray.extend（内存拷贝，微秒级）；
  缓冲达阈值（256KB ≈ 下行 8s）经 asyncio.to_thread 异步落盘
- I/O 异常只 warning 一次并整体停用，绝不影响下行/上行
- close 时补写 meta.json（帧数/字节数/起止 mono/采样率/帧长）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

FLUSH_BYTES = 256 * 1024


class PcmDumpSink:
    """下行/上行 PCM 原样落盘（append 模式，16k mono s16le）"""

    def __init__(self, prefix: str, sample_rate: int = 16000, frame_ms: int = 20) -> None:
        self._down_path = f"{prefix}.pcm"
        self._up_path = f"{prefix}.up.pcm"
        self._meta_path = f"{prefix}.meta.json"
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._down_buf = bytearray()
        self._up_buf = bytearray()
        self.down_frames = 0
        self.up_frames = 0
        self.down_bytes = 0
        self.up_bytes = 0
        self.started_mono = time.monotonic()
        self._warned = False
        self._closed = False
        self._flush_task: asyncio.Task | None = None
        self._down_fh = self._open(self._down_path)
        self._up_fh = self._open(self._up_path)

    # ---------- 音频路径（只做内存拷贝） ----------

    def write_down(self, pcm: bytes) -> None:
        if self._closed or self._down_fh is None:
            return
        self.down_frames += 1
        self.down_bytes += len(pcm)
        self._down_buf.extend(pcm)
        self._maybe_schedule_flush()

    def write_up(self, pcm: bytes) -> None:
        if self._closed or self._up_fh is None:
            return
        self.up_frames += 1
        self.up_bytes += len(pcm)
        self._up_buf.extend(pcm)
        self._maybe_schedule_flush()

    def _maybe_schedule_flush(self) -> None:
        if len(self._down_buf) < FLUSH_BYTES and len(self._up_buf) < FLUSH_BYTES:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return  # 已有落盘任务在跑，本轮缓冲留给下次
        # 事件循环内快照 + 清缓冲（微秒级），磁盘 I/O 全部交给 to_thread
        down, up = bytes(self._down_buf), bytes(self._up_buf)
        self._down_buf.clear()
        self._up_buf.clear()
        self._flush_task = asyncio.get_running_loop().create_task(
            asyncio.to_thread(self._write_sync, down, up)
        )

    # ---------- I/O（事件循环外） ----------

    def _warn_once(self, msg: str) -> None:
        if not self._warned:
            self._warned = True
            logger.warning("[lat] pcm dump disabled after error: %s", msg)

    def _open(self, path: str):
        try:
            return open(path, "ab")
        except OSError as e:
            self._warn_once(f"open {path} failed: {e}")
            return None

    def _write_sync(self, down: bytes, up: bytes) -> None:
        for fh, data in ((self._down_fh, down), (self._up_fh, up)):
            if fh is None or not data:
                continue
            try:
                fh.write(data)
            except OSError as e:
                self._warn_once(f"write failed: {e}")

    async def close(self) -> None:
        """冲刷剩余缓冲 + 写 meta + 关文件（I/O 在 to_thread，不阻塞事件循环）"""
        if self._closed:
            return
        self._closed = True
        ended_mono = time.monotonic()
        try:
            if self._flush_task is not None:
                try:
                    await self._flush_task
                except Exception as e:  # noqa: BLE001
                    self._warn_once(f"pending flush failed: {e}")
            down, up = bytes(self._down_buf), bytes(self._up_buf)
            self._down_buf.clear()
            self._up_buf.clear()
            await asyncio.to_thread(self._close_sync, down, up, ended_mono)
        except Exception as e:  # noqa: BLE001
            self._warn_once(f"close failed: {e}")

    def _close_sync(self, down: bytes, up: bytes, ended_mono: float) -> None:
        self._write_sync(down, up)
        meta = {
            "sample_rate": self._sample_rate,
            "frame_ms": self._frame_ms,
            "started_mono": self.started_mono,
            "ended_mono": ended_mono,
            "down": {"path": os.path.basename(self._down_path),
                     "frames": self.down_frames, "bytes": self.down_bytes},
            "up": {"path": os.path.basename(self._up_path),
                   "frames": self.up_frames, "bytes": self.up_bytes},
        }
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self._warn_once(f"meta write failed: {e}")
        for fh in (self._down_fh, self._up_fh):
            if fh is not None:
                try:
                    fh.close()
                except OSError as e:
                    self._warn_once(f"close file failed: {e}")
