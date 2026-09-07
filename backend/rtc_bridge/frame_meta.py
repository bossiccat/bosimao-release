"""DownFrame —— 下行帧 + 逐帧追溯元数据（F6/F7 观测）

背景：下行链路跨 4 个进程/边界（qwen delta → rtc_bridge 成帧 → WS →
sidecar → TRTC SDK → 手机播放）。此前每一跳的日志都只有孤立的字节数，
没有共享身份，因此「云端发了但手机没响」与「sidecar 收到但 SDK 没送出去」
在日志里无法区分——这是「卡断」类故障长期无法定位的根因。

本模块给每一帧挂上：
- reply_id  ：一轮回复的身份（bridge 本地铸造，新回复必变）
- frame_seq ：reply 内单调递增的帧序号（新 reply 归零）
- src_seq   ：产生本帧的云端 chunk 序号（一个 chunk 可产出多帧）
- enq_mono  ：入队 monotonic 秒（rtc_bridge 进程时钟）
- send_mono ：发送前 monotonic 秒（rtc_bridge 进程时钟）

时钟边界：enq_mono / send_mono 是 rtc_bridge 进程的 monotonic 时钟，
与 sidecar / 手机不是同一基准，跨进程相减无意义。只允许在同一进程内做差。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DownFrame:
    """一个下行 PCM 帧及其追溯身份（可变：send_mono 在发送前回填）"""

    payload: bytes
    reply_id: str = ""
    frame_seq: int = 0
    src_seq: int = -1
    enq_mono: float = 0.0
    send_mono: float = 0.0
    generation: int = 0

    @property
    def size(self) -> int:
        return len(self.payload)

    def trace_fields(self) -> dict:
        """WS 下发的追溯字段（可选字段，旧 sidecar 忽略即可）"""
        return {
            "reply_id": self.reply_id,
            "frame_seq": self.frame_seq,
            "src_seq": self.src_seq,
            "t_enq": round(self.enq_mono, 6),
            "t_send": round(self.send_mono, 6),
        }
