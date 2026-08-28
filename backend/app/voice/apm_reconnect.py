"""ReconnectScheduler —— 指数退避重连调度器（A8：重连失败不得永久静默）

ApmBridge 云端 ws 断开后的重试托管：
- 退避：base * 2^(n-1)，封顶 max（默认 1s/2s/4s/.../60s）
- 上限：连续 max_attempts 次失败 → 放弃并回调 on_give_up（上层经错误通道上报，
  手机端可感知"云端引擎断开"，不再无限静音）
- 成功：计数清零；放弃后不再接受调度（须由上层重建实例恢复）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

RECONNECT_BASE_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 60.0
RECONNECT_MAX_ATTEMPTS = 10


class ReconnectScheduler:
    """单实例单循环：schedule() 幂等；attempt 回调由使用方自行加锁完成重握手"""

    def __init__(
        self,
        attempt: Callable[[], Awaitable[bool]],
        on_give_up: Callable[[], Awaitable[None]],
        on_retry: Callable[[int, int, float], Awaitable[None]] | None = None,
        base_delay_s: float = RECONNECT_BASE_DELAY_S,
        max_delay_s: float = RECONNECT_MAX_DELAY_S,
        max_attempts: int = RECONNECT_MAX_ATTEMPTS,
    ) -> None:
        self._attempt = attempt
        self._on_give_up = on_give_up
        self._on_retry = on_retry
        self._base = base_delay_s
        self._max = max_delay_s
        self.max_attempts = max_attempts
        self._task: asyncio.Task | None = None
        self.failures = 0
        self.gave_up = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def schedule(self) -> bool:
        """启动重连循环（已在跑 / 已放弃则忽略）；返回是否本次启动"""
        if self.gave_up or self.running:
            return False
        self._task = asyncio.create_task(self._run())
        return True

    def cancel(self) -> None:
        """外部关闭（close()）时终止循环"""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次重试前的等待时长（指数退避，封顶）"""
        return min(self._base * (2 ** (attempt - 1)), self._max)

    async def _run(self) -> None:
        try:
            while True:
                self.failures += 1
                if self.failures > self.max_attempts:
                    self.gave_up = True
                    logger.error("reconnect gave up after %d attempts", self.max_attempts)
                    try:
                        await self._on_give_up()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("on_give_up callback failed: %s", e)
                    return
                delay = self.delay_for(self.failures)
                logger.warning("reconnect attempt %d/%d in %.1fs",
                               self.failures, self.max_attempts, delay)
                if self._on_retry is not None:
                    try:
                        await self._on_retry(self.failures, self.max_attempts, delay)
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(delay)
                if await self._attempt():
                    self.failures = 0
                    logger.info("reconnect succeeded")
                    return
        finally:
            self._task = None
