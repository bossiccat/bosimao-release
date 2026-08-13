"""device/IP/route 限流：固定窗口计数（SPEC §5 42901 / ADR-014）

device 键与 IP 键独立计数；换 token 不绕过 IP 限流，换 IP 不绕过 device 限流。
"""
from __future__ import annotations

import time as _time

from pydantic import BaseModel, Field

from .storage import VoiceStore


class RateLimitConfig(BaseModel):
    # 2026-08-13 高压 H3/H9 发现：60s 大窗口下，固定窗口计数对慢速低频滥用
    # 不敏感（速率 < 阈值永不触发）。窗口细化到 10s，device/ip 每分钟限额
    # 语义不变（10*6=60/min、20*6=120/min），更细粒度捕捉突发与慢速爬升。
    window_seconds: int = Field(default=10, ge=1)
    device_limit: int = Field(default=10, ge=1)
    ip_limit: int = Field(default=20, ge=1)


class RateLimiter:
    def __init__(self, store: VoiceStore, config: RateLimitConfig) -> None:
        self._store = store
        self.config = config

    def _window_start(self, now: float) -> float:
        window = float(self.config.window_seconds)
        return int(now // window) * window

    def check(self, subject_id: str, ip: str, route_key: str,
              now: float | None = None) -> tuple[bool, int | None]:
        """返回 (allowed, retry_after_seconds)；超限时 retry_after 为正整数"""
        ts = _time.time() if now is None else now
        window = self._window_start(ts)

        device_count = self._store.rate_limit.increment(
            f"device:{subject_id}", route_key, window, now=ts
        )
        if device_count > self.config.device_limit:
            return False, max(1, int(window + self.config.window_seconds - ts) + 1)

        ip_count = self._store.rate_limit.increment(
            f"ip:{ip}", route_key, window, now=ts
        )
        if ip_count > self.config.ip_limit:
            return False, max(1, int(window + self.config.window_seconds - ts) + 1)

        return True, None
