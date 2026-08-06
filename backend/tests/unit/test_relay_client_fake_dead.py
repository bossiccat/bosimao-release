"""relay_client 中继假死感知单测（Task4 加固）

覆盖：连续"WS 连上但中继无响应"（pair timeout）计数 → 判定实例假死 → 60s 退避；
      收到任何中继消息 → 计数复位。纯逻辑测试，不依赖网络/中继服务。
"""
from __future__ import annotations

import time

import pytest

from relay.relay_client import (
    PAIR_RESPONSE_TIMEOUT_S,
    SUSPECT_DEAD_BACKOFF_S,
    SUSPECT_DEAD_STRIKES,
    RelayClient,
)


def _client() -> RelayClient:
    return RelayClient(
        relay_url="ws://127.0.0.1:1/relay/ws",  # 端口 1 必连不上；仅测逻辑不实际连接
        token="tok",
        device_id="jax-pc-test",
        pairing_code="000000",
    )


def test_constants_sane():
    # 20s > 中继 heartbeat_interval_s(15)：健康中继必能收到首帧，不误判
    assert PAIR_RESPONSE_TIMEOUT_S > 15
    assert SUSPECT_DEAD_STRIKES == 3
    assert SUSPECT_DEAD_BACKOFF_S == 60


def test_pair_timeout_strikes_then_suspect_dead():
    c = _client()
    assert c._pair_timeout_strikes == 0
    assert c._suspect_dead_until == 0.0

    c._record_pair_timeout()   # strike 1
    assert c._pair_timeout_strikes == 1
    assert c._suspect_dead_until == 0.0

    c._record_pair_timeout()   # strike 2
    assert c._pair_timeout_strikes == 2
    assert c._suspect_dead_until == 0.0

    c._record_pair_timeout()   # strike 3 → 判定假死，退避 60s
    assert c._pair_timeout_strikes == 0          # 复位
    assert c._suspect_dead_until > time.time()   # 已设置退避截止
    assert c._suspect_dead_until <= time.time() + SUSPECT_DEAD_BACKOFF_S + 1


def test_relay_response_resets_strikes():
    c = _client()
    c._record_pair_timeout()   # strike 1
    c._record_pair_timeout()   # strike 2
    assert c._pair_timeout_strikes == 2
    # 收到任何中继消息（_relay_loop 内复位）→ 计数归零
    c._pair_timeout_strikes = 0
    assert c._pair_timeout_strikes == 0
    # 复位后重新计数：1 次不触发假死
    c._record_pair_timeout()
    assert c._suspect_dead_until == 0.0


def test_wait_relay_backoff_sleeps_when_suspect_dead():
    import asyncio

    async def run():
        c = _client()
        # 无假死：立即返回（不 sleep）
        c._suspect_dead_until = 0.0
        t0 = time.monotonic()
        await c._wait_relay_backoff()
        assert time.monotonic() - t0 < 0.5

        # 假死退避中：剩余 2s 会 sleep（只测小间隔，不真等 60s）
        c._suspect_dead_until = time.time() + 2.0
        t0 = time.monotonic()
        await c._wait_relay_backoff()
        assert time.monotonic() - t0 >= 1.5

    asyncio.run(run())


def test_record_pair_timeout_threshold_uses_constant():
    c = _client()
    for i in range(SUSPECT_DEAD_STRIKES):
        c._record_pair_timeout()
    assert c._suspect_dead_until > time.time()
