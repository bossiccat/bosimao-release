"""BoundedAudioQueue.flush() 丢弃必须**被显式记账**（#131 验收补测 · 任务 3）

为什么需要这个文件
------------------
`flush()`（bounded_audio_queue.py）是**打断路径**的唯一出口：
DownlinkShaper.reset() → self._q.flush()（shaper.py:113-115）。它只 **return** 丢弃数，
既不累加 `drops` 也不累加 `age_dropped` —— 对比 `_drop_expired`（两者都加）。

后果是一条结构性的观测盲区：
  · 打断时 shaper.reset() 丢掉的那批帧（实测一次 ≈158 帧 ≈3.16s）**不进任何计数器**；
  · 而语音质量门禁 `check-voice-quality-gate.py` 的「下行零丢帧」读的是桥侧
    `queue_drops_down`（health.py:93 → session.py:494 → 本队列的 drops）
    ⇒ **打断造成的丢弃在结构上不在它的可见域内**，门禁可以"零丢帧"通过，而实际
    有一次打断丢弃了 3 秒待播音频。
  · 同时 JS 侧 `downlink_pacer.js::clear()` **计入** `stats.dropped` 且有断言
    （barge-in-flush-exec.test.js）—— 两侧口径相反，对账时必然对不上。

口径选择（重要，不许"顺手统一"）
--------------------------------
新增**独立**计数 `flush_dropped`，**不并进 `drops`**：`drops` 的既有语义是
"背压/帧龄丢弃"（入队预算过载 + 帧龄过期），已经被 health/session 的
`queue_drops_down` 消费。把打断丢弃混进去会让既有消费方把"用户插话"误读成
"系统过载/丢帧故障"，也会让门禁的零丢帧判据含义漂移。`age_dropped` 同样不受影响。
"""
from __future__ import annotations

from rtc_bridge.bounded_audio_queue import BoundedAudioQueue

FRAME = 640


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_queue(max_frames: int = 200) -> BoundedAudioQueue:
    return BoundedAudioQueue(
        max_frames=max_frames,
        max_bytes=max_frames * FRAME,
        max_frame_age_ms=60000,
        now_fn=_Clock(),
    )


def test_flush_all_records_dropped_count_without_touching_other_counters():
    """flush() 丢掉的帧必须进 flush_dropped；drops / age_dropped 必须**纹丝不动**。"""
    q = _make_queue()
    for _ in range(100):
        q.push(b"a" * FRAME)

    assert q.depth == 100
    assert q.drops == 0
    assert q.age_dropped == 0

    # 打断：harness 等价于 DownlinkShaper.reset()
    assert q.flush() == 100, "flush() 返回值必须是被丢弃的真实帧数（既有契约）"
    assert q.depth == 0

    assert q.flush_dropped == 100, "打断丢弃必须被显式记账，否则门禁结构上看不见它"
    assert q.metrics()["flush_dropped"] == 100, "flush_dropped 必须暴露在 metrics() 里"
    # 口径隔离：drops 语义是"背压/帧龄丢弃"，被 queue_drops_down 消费，
    # 混入打断丢弃会把"用户插话"误报成"系统丢帧故障"。
    assert q.drops == 0, "打断丢弃不得混进 drops（会污染 queue_drops_down 的零丢帧门禁口径）"
    assert q.age_dropped == 0, "打断丢弃不是帧龄过期，不得混进 age_dropped"
    assert q.metrics()["queue_drops"] == 0
    assert q.metrics()["age_dropped"] == 0


def test_flush_on_empty_queue_does_not_inflate_counter():
    """空队列上 flush（打断可能落在任意时刻）不得虚增计数。"""
    q = _make_queue()
    q.push(b"a" * FRAME)
    assert q.flush() == 1
    assert q.flush_dropped == 1

    # 第二次、第三次打断：队列已空 → 丢弃 0 帧 → 计数不得变
    assert q.flush() == 0
    assert q.flush() == 0
    assert q.flush_dropped == 1, "空队列 flush 必须丢弃 0 帧、计数不得虚增"


def test_flush_counter_accumulates_across_repeated_interrupts():
    """多次打断必须累加（一次打断一帧不留就悄悄丢掉是另一种不可观测）。"""
    q = _make_queue()
    for burst in (30, 12, 158):  # 158 ≈ 实测一次打断丢掉 ≈3.16s 待播音频
        for _ in range(burst):
            q.push(b"a" * FRAME)
        assert q.flush() == burst
    assert q.flush_dropped == 200
    assert q.metrics()["flush_dropped"] == 200
    assert q.drops == 0 and q.age_dropped == 0


def test_generation_flush_also_recorded():
    """带 generation 的 flush（旧代际作废）同样走 flush() 的丢弃路径，必须一并记账。"""
    q = _make_queue()
    q.push(b"old" * 100, generation=0)
    q.push(b"new" * 100, generation=1)

    assert q.flush(generation=0) == 1
    assert q.flush_dropped == 1, "按代际 flush 也是一次真实丢弃，不得漏记"
    assert q.drops == 0 and q.age_dropped == 0

    # 第三次：目标代际已无条目 → 丢弃 0 → 不得虚增
    assert q.flush(generation=0) == 0
    assert q.flush_dropped == 1


def test_bump_generation_records_flush_dropped():
    """bump_generation() = flush() + 代际 +1；其内部 flush 的丢弃同样必须可见。"""
    q = _make_queue()
    for _ in range(7):
        q.push(b"a" * FRAME)

    assert q.bump_generation() == 1
    assert q.depth == 0
    assert q.flush_dropped == 7, "打断语义（bump_generation）的丢弃必须可见"
    assert q.drops == 0 and q.age_dropped == 0


def test_backpressure_and_age_drops_stay_in_drops_not_flush_dropped():
    """反向：背压/帧龄丢弃只进 drops，**不得**被记成 flush_dropped（防口径互换）。"""
    clock = _Clock()
    q = BoundedAudioQueue(max_frames=3, max_bytes=3 * FRAME,
                          max_frame_age_ms=100, now_fn=clock)
    for i in range(5):
        q.push(bytes([i]) * FRAME)          # max_frames=3 → 背压丢旧 2 帧
    assert q.drops == 2

    clock.advance(0.2)                       # 全部超龄
    assert q.pop() is None                   # 帧龄丢弃 3 帧
    assert q.age_dropped == 3
    assert q.drops == 5

    assert q.flush_dropped == 0, "背压/帧龄丢弃不得被记成 flush_dropped（两个口径必须互斥）"
    assert q.metrics()["flush_dropped"] == 0
