"""BoundedAudioQueue 有界背压验收测试（SPEC §11.1 / AC-10 / QA spec 5.5）

- 条目携带 generation/created_at/size
- 入队同时检查 max_frames / max_bytes / 最大帧龄
- 过载丢旧保新，记录 queue_depth / high_watermark / drops / backpressure_events
- generation flush：旧 generation 条目不再被消费
"""
from __future__ import annotations

import time

from rtc_bridge.bounded_audio_queue import BoundedAudioQueue, QueueEntry

FRAME = 640


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_max_frames_drops_oldest_keeps_newest():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=3, max_bytes=10 * FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    for i in range(5):
        assert q.push(bytes([i]) * FRAME) is True
    assert q.depth == 3
    assert q.drops == 2
    assert q.backpressure_events == 2
    # 丢旧保新：剩余的是最后 3 个
    payloads = [q.pop().payload[0] for _ in range(3)]
    assert payloads == [2, 3, 4]
    assert q.pop() is None


def test_max_bytes_bound_enforced():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=100, max_bytes=3 * FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    for i in range(5):
        q.push(bytes([i]) * FRAME)
    assert q.bytes_total <= 3 * FRAME
    assert q.depth == 3
    assert q.drops == 2


def test_max_frame_age_expired_entries_dropped():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=100, max_bytes=100 * FRAME,
                          max_frame_age_ms=1000, now_fn=clock)
    q.push(b"a" * FRAME)
    clock.advance(0.5)
    q.push(b"b" * FRAME)
    clock.advance(0.6)  # 第一帧超龄（>1000ms）
    entry = q.pop()
    assert entry is not None and entry.payload[0] == ord("b")
    assert q.drops == 1


def test_generation_flush_removes_old_generation():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=100, max_bytes=100 * FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    q.push(b"old" * 100, generation=0)
    q.push(b"new" * 100, generation=1)
    flushed = q.flush(generation=0)
    assert flushed == 1
    remaining = q.pop()
    assert remaining is not None and remaining.generation == 1
    assert q.pop() is None


def test_flush_all_clears_queue():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=10, max_bytes=10 * FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    q.push(b"a" * FRAME)
    q.push(b"b" * FRAME)
    assert q.flush() == 2
    assert q.depth == 0
    assert q.pop() is None


def test_metrics_depth_high_watermark_drops_backpressure():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=2, max_bytes=2 * FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    q.push(b"a" * FRAME)
    q.push(b"b" * FRAME)
    assert q.high_watermark == 2
    q.push(b"c" * FRAME)  # 触发丢旧保新
    assert q.depth == 2
    assert q.drops == 1
    assert q.backpressure_events == 1
    assert q.high_watermark == 2  # 高位水线不回退


def test_single_frame_oversize_rejected():
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=2, max_bytes=FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    ok = q.push(b"x" * (FRAME + 1))
    assert ok is False
    assert q.depth == 0
    assert q.drops == 1


def test_entry_carries_generation_created_at_size():
    clock = _Clock(start=500.0)
    q = BoundedAudioQueue(max_frames=5, max_bytes=5 * FRAME,
                          max_frame_age_ms=60000, now_fn=clock)
    q.push(b"g" * FRAME, generation=7)
    entry = q.pop()
    assert isinstance(entry, QueueEntry)
    assert entry.generation == 7
    assert entry.created_at == 500.0
    assert entry.size == FRAME
