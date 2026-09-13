"""下行整段回复不得丢帧（根因修复 2026-09-13）

背景
----
用户抱怨模型回复像 3 倍速、吐字不清、话讲不完。根因在**下行有界队列的预算尺度**：
模型把整段回复的音频以远快于实时的突发推下来（实测 6.88s 音频约 1.5s 墙钟到齐），
而 DownlinkShaper 严格按实时 50 帧/s 出队（shaper.py:137-138）。于是帧在队列里停留
>1s 就被 `max_frame_age_ms=1000` 当「陈旧遥测」丢弃，`max_frames=200`（=4s）也装不下
一整段回复 ⇒ 模型产出 6.88s、手机只收到 4.26s，约 38% 音频按**整帧**被丢。

设计错误：`max_frame_age_ms` 把**下行音频内容**当成了陈旧遥测。对下行而言，「早到」
的帧不是陈旧，恰恰是要播的内容；帧龄过期只对**上行**有意义（迟到的上行帧确实没用）。

铁律「依赖注入的替身覆盖率 ≠ 默认路径覆盖率」
------------------------------------------------
本文件**不注入**任何自定义队列预算参数：一律从 `BridgeConfig()` 与
`PeerVoiceSession.__init__` 的**真实默认值**读取，再据此构造队列。注入的只有假时钟
（它只替换时间源，不替换被测的预算逻辑）。
"""
from __future__ import annotations

import inspect

from rtc_bridge.bounded_audio_queue import BoundedAudioQueue
from rtc_bridge.config import BridgeConfig
from rtc_bridge.session import PeerVoiceSession

FRAME_BYTES = 640          # 16k mono s16 × 20ms
FRAME_MS = 20


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _config_down_defaults() -> tuple[int, int, int]:
    cfg = BridgeConfig()
    return cfg.down_max_frames, cfg.down_max_bytes, cfg.down_max_frame_age_ms


def _session_down_defaults() -> tuple[int, int, int]:
    params = inspect.signature(PeerVoiceSession.__init__).parameters
    return (
        params["down_max_frames"].default,
        params["down_max_bytes"].default,
        params["down_max_frame_age_ms"].default,
    )


def test_session_and_config_down_defaults_agree():
    """session.py 与 config.py 的下行默认值必须来自同一组常量（防两处漂移）。"""
    assert _session_down_defaults() == _config_down_defaults()


def test_full_reply_burst_fully_consumed_with_default_budget():
    """生产者瞬时推一整段 7s 回复（350 帧）+ 消费者按实时 50 帧/s 出队 → 一帧都不能丢。

    RED（修复前）：max_frames=200 装不下 350 帧、且 max_frame_age_ms=1000 把停留
    >1s 的帧当陈旧数据丢弃 ⇒ consumed < 350、drops > 0、age_dropped > 0。
    """
    clock = _Clock()
    max_frames, max_bytes, max_age_ms = _config_down_defaults()
    q = BoundedAudioQueue(
        max_frames=max_frames,
        max_bytes=max_bytes,
        max_frame_age_ms=max_age_ms,
        now_fn=clock,
    )

    burst = 350  # 7.0s @ 20ms
    for i in range(burst):
        assert q.push(bytes([i % 251]) * FRAME_BYTES) is True

    consumed = 0
    for _ in range(burst):
        clock.advance(FRAME_MS / 1000.0)   # 真实出队节拍：50 帧/s
        if q.pop() is not None:
            consumed += 1

    assert consumed == burst, f"整段回复必须一帧不丢：consumed={consumed} burst={burst}"
    assert q.drops == 0, f"下行整段回复不应产生任何丢帧：drops={q.drops}"
    assert q.age_dropped == 0, f"下行早到帧不是陈旧数据：age_dropped={q.age_dropped}"


def test_upstream_queue_still_drops_stale_frames():
    """保护没有被全局关掉：上行迟到帧仍然必须按帧龄丢弃。"""
    clock = _Clock()
    cfg = BridgeConfig()
    q = BoundedAudioQueue(
        max_frames=cfg.up_max_frames,
        max_bytes=cfg.up_max_bytes,
        max_frame_age_ms=cfg.up_max_frame_age_ms,
        now_fn=clock,
    )
    assert q.push(b"a" * FRAME_BYTES) is True
    clock.advance((cfg.up_max_frame_age_ms + 500) / 1000.0)
    assert q.pop() is None, "超龄上行帧必须被丢弃"
    assert q.age_dropped >= 1


def test_flush_and_bump_generation_still_clear_immediately():
    """barge-in 冲洗语义必须仍然立刻清空（即便下行预算被放大）。"""
    clock = _Clock()
    max_frames, max_bytes, max_age_ms = _config_down_defaults()
    q = BoundedAudioQueue(
        max_frames=max_frames,
        max_bytes=max_bytes,
        max_frame_age_ms=max_age_ms,
        now_fn=clock,
    )
    for _ in range(100):
        q.push(b"z" * FRAME_BYTES)
    assert q.flush() == 100
    assert q.depth == 0 and q.pop() is None

    for _ in range(10):
        q.push(b"y" * FRAME_BYTES)
    gen_before = q.generation
    assert q.bump_generation() == gen_before + 1
    assert q.depth == 0 and q.pop() is None
