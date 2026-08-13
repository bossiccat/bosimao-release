"""PcmFrameBuffer 跨块 residue 验收测试（SPEC §11.1 / AC-08 / AC-09 / QA spec 5.5）

- 跨块保留 residue，只输出完整 640-byte 帧
- 639/640/641 bytes 边界
- 会话尾部不足帧显式补零（pad）或丢弃（drop）并记录指标
"""
from __future__ import annotations

import pytest

from rtc_bridge.frame_buffer import PcmFrameBuffer

FRAME = 640


def test_residue_is_preserved_across_chunks():
    buffer = PcmFrameBuffer(frame_bytes=FRAME)
    assert buffer.feed(b"a" * 300) == []
    assert buffer.feed(b"b" * 340) == [b"a" * 300 + b"b" * 340]


def test_639_640_641_boundaries():
    buffer = PcmFrameBuffer(frame_bytes=FRAME)
    # 639B：不足一帧，只留 residue
    assert buffer.feed(b"x" * 639) == []
    assert buffer.pending() == 639
    # 1B：凑满 640B → 完整帧
    assert buffer.feed(b"y") == [b"x" * 639 + b"y"]
    # 641B：一个完整帧 + 1B residue
    frames = PcmFrameBuffer(frame_bytes=FRAME).feed(b"z" * 641)
    assert len(frames) == 1
    assert len(frames[0]) == FRAME


def test_tail_drop_mode_records_metric():
    buffer = PcmFrameBuffer(frame_bytes=FRAME, tail_mode="drop")
    buffer.feed(b"a" * FRAME)          # 完整帧
    buffer.feed(b"b" * 300)            # 不足帧 residue
    assert buffer.flush() == []
    assert buffer.tail_dropped_bytes == 300
    assert buffer.total_frames == 1
    assert buffer.pending() == 0


def test_tail_pad_mode_outputs_padded_frame():
    buffer = PcmFrameBuffer(frame_bytes=FRAME, tail_mode="pad")
    buffer.feed(b"c" * 500)
    frames = buffer.flush()
    assert len(frames) == 1
    assert len(frames[0]) == FRAME
    assert frames[0][:500] == b"c" * 500
    assert frames[0][500:] == b"\x00" * 140
    assert buffer.tail_padded_frames == 1
    assert buffer.pending() == 0


def test_flush_with_no_pending_is_noop():
    buffer = PcmFrameBuffer(frame_bytes=FRAME)
    assert buffer.flush() == []
    assert buffer.tail_dropped_bytes == 0


def test_reset_clears_pending_without_output():
    buffer = PcmFrameBuffer(frame_bytes=FRAME)
    buffer.feed(b"d" * 300)
    buffer.reset()
    assert buffer.pending() == 0
    assert buffer.feed(b"e" * FRAME) == [b"e" * FRAME]


def test_multiple_chunks_accumulate_to_multiple_frames():
    buffer = PcmFrameBuffer(frame_bytes=FRAME)
    frames = []
    for i in range(3):
        frames.extend(buffer.feed(b"f" * FRAME))
    assert len(frames) == 3
    assert all(len(f) == FRAME for f in frames)
    assert buffer.total_frames == 3
