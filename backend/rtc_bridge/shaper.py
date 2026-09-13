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
import time
from typing import Awaitable, Callable

from .bounded_audio_queue import BoundedAudioQueue
from .frame_buffer import PcmFrameBuffer
from .frame_meta import DownFrame

logger = logging.getLogger(__name__)

# 默认下行预算（AC-10）。尺度说明见 config.py 的 down_* 注释：模型突发式下发 +
# 实时 50 帧/s 出队 ⇒ 队列须能装下一整段回复（1500 帧 × 20ms = 30s），早到的帧是
# 待播内容而非陈旧数据。必须与 config.py / session.py 保持同一组尺度。
DEFAULT_DOWN_MAX_FRAMES = 1500
DEFAULT_DOWN_MAX_BYTES = 1500 * 640
DEFAULT_DOWN_MAX_FRAME_AGE_MS = 30000


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
        self._loop = asyncio.get_event_loop()  # 绝对时间表节拍用（monotonic clock）
        # P0-5/F3：每 reply 首帧观测（push 首帧 / send 首帧）
        self._last_push_ts = 0.0
        self._last_send_ts = 0.0
        # F6/F7：当前 reply 身份与帧序号（begin_reply 时重置）
        self._reply_id = ""
        self._frame_seq = 0

    def begin_reply(self, reply_id: str) -> None:
        """开始一轮新回复：切换 reply_id 并把 frame_seq 归零

        由 PeerVoiceSession 在 reply 边界调用（下行静默后新音频到达）。
        """
        self._reply_id = reply_id
        self._frame_seq = 0

    @property
    def current_reply_id(self) -> str:
        return self._reply_id

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def push(self, pcm: bytes, src_seq: int = -1) -> None:
        """ApmBridge.on_audio_out 回调入口（非阻塞：跨块拆帧 + 有界入队）

        src_seq：本 chunk 的来源序号（F6/F7）；一个变长 chunk 可产出多帧，
        产出的每一帧都继承该 src_seq，帧序号 frame_seq 在本 reply 内递增。
        """
        if self._closed:
            return
        now = time.monotonic()
        if now - self._last_push_ts > 0.5:
            # P0-5/F3：每 reply 首帧 push 时刻（确认本地下行零延迟）
            logger.info("[lat] down first push mono=%.3f bytes=%d", now, len(pcm))
        self._last_push_ts = now
        frames = self._buffer.feed(pcm, src_seq=src_seq)
        srcs = self._buffer.last_src_seqs
        for payload, src in zip(frames, srcs):
            seq = self._frame_seq
            self._frame_seq += 1
            self._q.push(payload, reply_id=self._reply_id, frame_seq=seq, src_seq=src)
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
        # 绝对时间表节拍（v0.6.7 P0 修复卡顿）：目标发送时刻 t_n = t0 + n*frame_s。
        # 原 sleep(frame_s) 在 Windows 上精度 ~15.6ms，每帧实睡 30ms+ → 下行速率仅
        # 60-70% 标称值，手机端 jitter buffer 欠载 → 周期性卡顿。改为按绝对时间补偿：
        # 落后则连发追赶（不睡），超前则睡到目标时刻，长期速率精确锁频。
        t0 = None
        n = 0
        while not self._closed:
            entry = self._q.pop()
            if entry is None:
                self._wake.clear()
                await self._wake.wait()
                t0 = None  # 空转后重建基准（防长时间积压基准漂移）
                n = 0
                continue
            if self._closed:
                return
            now = self._loop.time()
            if t0 is None:
                t0 = now
                n = 0
            target = t0 + n * self._frame_s
            n += 1
            lag = now - target
            if lag < -0.002:  # 超前 >2ms：睡到目标时刻（一次性补偿，无累积误差）
                await asyncio.sleep(-lag)
            # lag >= 0：已落后（消费慢/网络抖动），立即发不睡，靠后续帧追赶
            now_mono = time.monotonic()
            frame = DownFrame(
                payload=entry.payload,
                reply_id=entry.reply_id,
                frame_seq=entry.frame_seq,
                src_seq=entry.src_seq,
                enq_mono=entry.created_at,
                generation=entry.generation,
            )
            if now_mono - self._last_send_ts > 0.5:
                # P0-5/F3：每 reply 首帧 send 时刻
                logger.info(
                    "[lat] down first send mono=%.3f reply=%s seq=%s src=%s age_ms=%.1f",
                    now_mono, entry.reply_id or "-", entry.frame_seq, entry.src_seq,
                    (now_mono - entry.created_at) * 1000.0,
                )
            self._last_send_ts = now_mono
            try:
                frame.send_mono = time.monotonic()
                await self._send_frame(frame)
            except Exception as e:  # noqa: BLE001 - sidecar 断线不阻塞整形器
                logger.warning("shaper send frame failed: %s", e)

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
