"""BoundedAudioQueue —— 有界音频队列（SPEC §11.1 / AC-10）

条目携带 generation / created_at / size；入队同时检查 max_frames / max_bytes /
最大帧龄；过载丢旧保新；记录 queue_depth / high_watermark / drops /
backpressure_events 指标。音频回调只做非阻塞入队。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class QueueEntry:
    payload: bytes
    generation: int
    created_at: float
    size: int


class BoundedAudioQueue:
    def __init__(self, max_frames: int, max_bytes: int,
                 max_frame_age_ms: int, now_fn=time.monotonic) -> None:
        if max_frames <= 0 or max_bytes <= 0 or max_frame_age_ms <= 0:
            raise ValueError("队列预算必须为正")
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.max_frame_age_ms = max_frame_age_ms
        self._now = now_fn
        self._entries: deque[QueueEntry] = deque()
        self.generation = 0
        # 指标
        self.high_watermark = 0
        self.drops = 0
        self.backpressure_events = 0
        self.enqueued = 0

    # ---- 指标 ----

    @property
    def depth(self) -> int:
        return len(self._entries)

    @property
    def bytes_total(self) -> int:
        return sum(entry.size for entry in self._entries)

    def metrics(self) -> dict:
        return {
            "queue_depth": self.depth,
            "queue_high_watermark": self.high_watermark,
            "queue_drops": self.drops,
            "backpressure_events": self.backpressure_events,
            "queue_bytes": self.bytes_total,
        }

    # ---- 入队（非阻塞） ----

    def push(self, payload: bytes, generation: int | None = None) -> bool:
        """入队；过载丢旧保新；返回是否入队成功"""
        now = self._now()
        self._drop_expired(now)
        entry = QueueEntry(
            payload=payload,
            generation=self.generation if generation is None else generation,
            created_at=now,
            size=len(payload),
        )
        if entry.size > self.max_bytes:
            self.drops += 1
            self.backpressure_events += 1
            return False
        while self._entries and (
            self.depth >= self.max_frames or self.bytes_total + entry.size > self.max_bytes
        ):
            self._entries.popleft()
            self.drops += 1
            self.backpressure_events += 1
        self._entries.append(entry)
        self.enqueued += 1
        if self.depth > self.high_watermark:
            self.high_watermark = self.depth
        return True

    # ---- 出队 ----

    def pop(self) -> QueueEntry | None:
        """取出最旧未过期条目；空或全过期返回 None"""
        self._drop_expired(self._now())
        return self._entries.popleft() if self._entries else None

    def peek_oldest_created_at(self) -> float | None:
        return self._entries[0].created_at if self._entries else None

    # ---- generation flush ----

    def flush(self, generation: int | None = None) -> int:
        """丢弃指定 generation（None=全部）条目，返回丢弃数（旧 generation 不再消费）"""
        if generation is None:
            dropped = len(self._entries)
            self._entries.clear()
            return dropped
        kept: deque[QueueEntry] = deque(
            entry for entry in self._entries if entry.generation != generation
        )
        dropped = len(self._entries) - len(kept)
        self._entries = kept
        return dropped

    def bump_generation(self) -> int:
        """打断语义：清空队列并提升代际，旧帧自然失效"""
        self.flush()
        self.generation += 1
        return self.generation

    # ---- 内部 ----

    def _drop_expired(self, now: float) -> None:
        limit_ms = float(self.max_frame_age_ms)
        while self._entries and (now - self._entries[0].created_at) * 1000.0 > limit_ms:
            self._entries.popleft()
            self.drops += 1
