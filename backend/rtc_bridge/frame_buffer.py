"""PcmFrameBuffer —— 固定 20ms PCM 帧缓冲（SPEC §11.1 / AC-08 / AC-09）

跨块保留 residue，只输出完整 frame_bytes（默认 640B @16k s16 20ms）帧；
会话尾部不足帧按配置显式补零（pad）或丢弃（drop），并记录指标。
"""
from __future__ import annotations

TAIL_DROP = "drop"
TAIL_PAD = "pad"


class PcmFrameBuffer:
    def __init__(self, frame_bytes: int = 640, tail_mode: str = TAIL_DROP) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes 必须为正整数")
        if tail_mode not in (TAIL_DROP, TAIL_PAD):
            raise ValueError("tail_mode 只能是 drop 或 pad")
        self.frame_bytes = frame_bytes
        self.tail_mode = tail_mode
        self._buf = bytearray()
        # 指标
        self.total_frames = 0
        self.tail_dropped_bytes = 0
        self.tail_padded_frames = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """输入任意长度 PCM 块，返回本块产生的完整帧（不含不足帧）"""
        if not chunk:
            return []
        self._buf.extend(chunk)
        frames: list[bytes] = []
        while len(self._buf) >= self.frame_bytes:
            frames.append(bytes(self._buf[: self.frame_bytes]))
            del self._buf[: self.frame_bytes]
        self.total_frames += len(frames)
        return frames

    def flush(self) -> list[bytes]:
        """会话结束：显式处理不足帧（drop 记字节数 / pad 补零输出），清空缓冲"""
        if not self._buf:
            return []
        if self.tail_mode == TAIL_PAD:
            tail = bytes(self._buf) + b"\x00" * (self.frame_bytes - len(self._buf))
            self.tail_padded_frames += 1
            self.total_frames += 1
            self._buf.clear()
            return [tail]
        self.tail_dropped_bytes += len(self._buf)
        self._buf.clear()
        return []

    def reset(self) -> None:
        """丢弃未输出 residue（远端重进/打断时防串话）"""
        self._buf.clear()

    def pending(self) -> int:
        return len(self._buf)
