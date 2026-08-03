"""指标采集（帧延迟、推理耗时、误判计数、推送成功率）"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """轻量内存指标（MVP 够用；后续可接 Prometheus）"""

    frame_latencies: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    analysis_ms: deque[int] = field(default_factory=lambda: deque(maxlen=500))
    false_alert_count: int = 0
    push_total: int = 0
    push_ok: int = 0
    started_at: float = field(default_factory=time.time)

    def record_frame(self, latency_ms: float) -> None:
        self.frame_latencies.append(latency_ms)

    def record_analysis(self, ms: int) -> None:
        self.analysis_ms.append(ms)

    def record_alert(self, is_false: bool = False) -> None:
        if is_false:
            self.false_alert_count += 1

    def record_push(self, ok: bool) -> None:
        self.push_total += 1
        if ok:
            self.push_ok += 1

    def percentile(self, values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * pct / 100))
        return ordered[idx]

    def summary(self) -> dict:
        frames = list(self.frame_latencies)
        analysis = list(self.analysis_ms)
        return {
            "uptime_seconds": time.time() - self.started_at,
            "frame_latency_p50_ms": self.percentile(frames, 50),
            "frame_latency_p95_ms": self.percentile(frames, 95),
            "analysis_p50_ms": self.percentile(analysis, 50),
            "analysis_p95_ms": self.percentile(analysis, 95),
            "false_alert_count": self.false_alert_count,
            "push_total": self.push_total,
            "push_ok": self.push_ok,
        }


metrics = Metrics()
