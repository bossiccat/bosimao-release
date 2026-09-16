"""BoundedAudioQueue —— 有界音频队列（SPEC §11.1 / AC-10）

条目携带 generation / created_at / size；入队同时检查 max_frames / max_bytes /
最大帧龄；过载丢旧保新；记录 queue_depth / high_watermark / drops /
backpressure_events 指标。音频回调只做非阻塞入队。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueEntry:
    payload: bytes
    generation: int
    created_at: float
    size: int
    # F6/F7 逐帧追溯（可选；默认空表示未知，旧调用点不受影响）
    reply_id: str = ""
    frame_seq: int = -1
    src_seq: int = -1


class BoundedAudioQueue:
    def __init__(self, max_frames: int, max_bytes: int,
                 max_frame_age_ms: int, now_fn=time.monotonic,
                 name: str = "") -> None:
        if max_frames <= 0 or max_bytes <= 0 or max_frame_age_ms <= 0:
            raise ValueError("队列预算必须为正")
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.max_frame_age_ms = max_frame_age_ms
        # 队列身份（"up"/"down"）。丢帧归因：此前 age-drop WARNING 不带方向，
        # 一个 58 的 queue_drops 无法判断是上行还是下行丢的（2026-09-13 实测）。
        # 只加归因，不改任何丢弃行为/预算数值。
        self.name = name
        self._now = now_fn
        self._entries: deque[QueueEntry] = deque()
        self.generation = 0
        # 指标
        self.high_watermark = 0
        self.drops = 0
        self.backpressure_events = 0
        self.enqueued = 0
        # 帧龄丢弃计数：超龄条目在每次 push/pop 时被静默丢弃（原实现无任何日志），
        # 是「max_frames 形同虚设、有效缓冲实际只有约 1s」这一事实的唯一可观测入口。
        self.age_dropped = 0
        # 打断丢弃计数（flush() 路径）。**刻意独立于 drops**，理由见 flush() 的注释：
        # drops 的语义是"背压 / 帧龄丢弃"，且已被 health/session 的 queue_drops_down
        # 消费（health.py:93 → session.py:494）；把打断丢弃混进去会把"用户插话"
        # 误读成"系统过载/丢帧故障"，也会让门禁的"下行零丢帧"判据含义漂移。
        # 此前 flush() 丢弃的帧**不进任何计数器**（一次打断实测 ≈158 帧 ≈3.16s），
        # 是打断延迟/丢帧归因的一个结构性盲区。
        self.flush_dropped = 0
        self._last_age_drop_log = float("-inf")  # 首帧过龄即打，之后 ≥1s 一条
        # P0-5/F9：帧龄分布采样（量化 U4/U5 停摆导致的丢帧）
        self._age_samples: deque[float] = deque(maxlen=1000)
        self._pops = 0

    # ---- 指标 ----

    @property
    def depth(self) -> int:
        return len(self._entries)

    @property
    def bytes_total(self) -> int:
        return sum(entry.size for entry in self._entries)

    def metrics(self) -> dict:
        return {
            "queue_name": self.name,
            "queue_depth": self.depth,
            "queue_high_watermark": self.high_watermark,
            "queue_drops": self.drops,
            "backpressure_events": self.backpressure_events,
            "queue_bytes": self.bytes_total,
            "age_dropped": self.age_dropped,
            # 打断丢弃（flush 路径）单独曝光：它**不在** queue_drops 里，也不在
            # age_dropped 里（见 flush() 的口径说明）。消费方要与 queue_drops
            # 分别判读，否则会把"打断"与"过载/过龄"混为一谈。
            "flush_dropped": self.flush_dropped,
        }

    # ---- 入队（非阻塞） ----

    def push(self, payload: bytes, generation: int | None = None, *,
             reply_id: str = "", frame_seq: int = -1, src_seq: int = -1) -> bool:
        """入队；过载丢旧保新；返回是否入队成功

        reply_id / frame_seq / src_seq 为 F6/F7 逐帧追溯元数据，可选。
        """
        now = self._now()
        self._drop_expired(now)
        entry = QueueEntry(
            payload=payload,
            generation=self.generation if generation is None else generation,
            created_at=now,
            size=len(payload),
            reply_id=reply_id,
            frame_seq=frame_seq,
            src_seq=src_seq,
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
        now = self._now()
        self._drop_expired(now)
        if not self._entries:
            return None
        entry = self._entries.popleft()
        # P0-5/F9：pop 时帧龄采样，每 500 帧打 p50/p99
        self._age_samples.append((now - entry.created_at) * 1000.0)
        self._pops += 1
        if self._pops % 500 == 0 and self._age_samples:
            samples = sorted(self._age_samples)
            # 队列身份：旧实现硬编码 "up_audio"，下行队列也打 up_audio，无法归因。
            tag = f"{self.name}_audio" if self.name else "audio"
            logger.info("[lat] %s frame age ms p50=%.0f p99=%.0f drops=%d",
                        tag,
                        samples[len(samples) // 2],
                        samples[min(len(samples) - 1, int(len(samples) * 0.99))],
                        self.drops)
        return entry

    def peek_oldest_created_at(self) -> float | None:
        return self._entries[0].created_at if self._entries else None

    # ---- generation flush ----

    def flush(self, generation: int | None = None) -> int:
        """丢弃指定 generation（None=全部）条目，返回丢弃数（旧 generation 不再消费）

        丢弃数同时累加到**独立的** `flush_dropped`（并曝光为 metrics() 的同名字段）
        与 age_dropped 是两类不同的丢弃，混算会让既有消费方误读：

        · `drops` 的既有语义是"**背压 / 帧龄丢弃**"（入队预算过载 + 超龄过期），
          已被 health.py:93 / session.py:494 作为 `queue_drops_down` 消费，也是门禁
          「下行零丢帧」的读数。flush 是**打断**语义（DownlinkShaper.reset()），
          把它并进 drops 会让"用户插话"被误报成"系统过载/丢帧故障"。
        · `age_dropped` 是帧龄过期专项，flush 与帧龄无关，同样不得混入。

        ⇒ 新增独立计数 `flush_dropped`：既让打断丢弃**可观测**（此前一次打断丢弃
        ≈158 帧 ≈3.16s 完全不进任何计数器），又不改变任何既有口径。
        注意：JS 侧 downlink_pacer.js::clear() 是把打断丢弃计入 stats.dropped 的 ——
        两侧口径本就相反，本字段只解决"桥侧看得见"，不去动 JS 侧既有语义。
        """
        if generation is None:
            dropped = len(self._entries)
            self._entries.clear()
            self.flush_dropped += dropped
            return dropped
        kept: deque[QueueEntry] = deque(
            entry for entry in self._entries if entry.generation != generation
        )
        dropped = len(self._entries) - len(kept)
        self._entries = kept
        self.flush_dropped += dropped
        return dropped

    def bump_generation(self) -> int:
        """打断语义：清空队列并提升代际，旧帧自然失效"""
        self.flush()
        self.generation += 1
        return self.generation

    # ---- 内部 ----

    def _drop_expired(self, now: float) -> None:
        """丢弃超过 max_frame_age_ms 的队首条目。

        帧龄过期丢弃的**语义按方向不同**，调用方必须把 max_frame_age_ms 配对好：

        · 上行（手机→桥）：迟到帧确实没有价值 ⇒ 帧龄过期丢弃是对的，保持 1s 尺度。
        · 下行（桥→手机）：**早到不是陈旧**。模型以突发方式一次推下整段回复，而
          shaper 按实时 50 帧/s 出队，所以「排队等待播放」的帧天然会停留数秒 ——
          把 max_frame_age_ms 当陈旧判据会按整帧切掉待播内容（2026-09-13 实测：
          模型产出 6.88s、手机只收到 4.26s，约 38% 被丢 ⇒「3 倍速 + 吐字不清」）。
          正确修法是把下行 max_frame_age_ms 提到「整段回复」尺度（见 config.py 的
          down_* 注释），**不是**去掉这道保护 —— 超限仍丢旧 + 计数 + 日志。

        计数 `age_dropped` + 节流日志（≥1s 一条，避免高频丢帧刷爆日志）用于让
        「有效缓冲实际有多大」在运行期可见。
        """
        limit_ms = float(self.max_frame_age_ms)
        dropped_in_call = 0
        head_age_ms = 0.0
        while self._entries and (now - self._entries[0].created_at) * 1000.0 > limit_ms:
            if dropped_in_call == 0:
                head_age_ms = (now - self._entries[0].created_at) * 1000.0
            self._entries.popleft()
            self.drops += 1
            self.age_dropped += 1
            dropped_in_call += 1
        if dropped_in_call and now - self._last_age_drop_log >= 1.0:
            self._last_age_drop_log = now
            logger.warning(
                "[lat] audio age-drop[%s]: 丢弃 %d 帧 / 队首帧龄 %.0fms / 上限 %dms"
                "（age_dropped=%d，累计丢帧=%d）",
                self.name or "-", dropped_in_call, head_age_ms, self.max_frame_age_ms,
                self.age_dropped, self.drops,
            )
