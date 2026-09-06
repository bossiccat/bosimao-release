"""VoiceIntentRouter —— APM 文本 delta 累积器 + flush 路由器

解决"伪智能体"问题：APM (MiniCPM-o Realtime) 产出的 AI 文本回复
(response.output.delta kind=text) 之前只做 logger.info。
VoiceIntentRouter 将 delta 累积成完整回复，在 AI 说完（音频静默）
时 flush 到 Brain 做意图提取和任务路由。

职责边界：
- 只管累积 + flush，不做意图分类（Brain 负责）
- 不依赖 BrainPipeline、DeepSeek、本地 9B —— 纯文本累积器
- 路由决策（direct_reply/delegate/clarify/deny）由上层 session + Brain 完成

用法（在 PeerVoiceSession._on_text 中）：
    router = VoiceIntentRouter(on_route=self._route_to_brain)
    # APM text delta
    async def _on_text(self, text):
        await self._router.feed(text)
    # AI 说完（音频静默 >600ms）
    def _check_down_speaking_over(self):
        if self._down_speaking and now - self._last_down_ts > 0.6:
            asyncio.create_task(self._router.flush())
"""
from __future__ import annotations

import asyncio
import logging

from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# 默认缓冲上限：AI 回复一般 <500 字，2000 字是安全阀（防异常长输出堆积）
DEFAULT_MAX_BUFFER = 2000


class VoiceIntentRouter:
    """累积 APM 文本 delta，flush 时路由完整回复到 on_route 回调。

    线程安全：asyncio 单线程事件循环，feed/flush 顺序确定（无需锁）。
    """

    def __init__(
        self,
        on_route: Callable[[str], Awaitable[None]],
        max_buffer: int = DEFAULT_MAX_BUFFER,
    ) -> None:
        self._on_route = on_route
        self._max_buffer = max_buffer
        self._buffer: str = ""

    @property
    def buffered_text(self) -> str:
        """当前缓冲区文本（测试/观测用）"""
        return self._buffer

    async def feed(self, text: str) -> None:
        """喂入一个文本 delta（APM response.output.delta kind=text 的片段）。

        达到 max_buffer 自动 flush（防异常长输出内存泄漏）。
        """
        if not text:
            return
        self._buffer += text
        if len(self._buffer) >= self._max_buffer:
            logger.info(
                "voice intent buffer hit max (%d), auto-flushing",
                self._max_buffer,
            )
            # P0（2026-09-06 21:06 实锤）：自动 flush 会同步走 brain API
            # （超时 5~8s）——必须 create_task，保证任何路径（recv 链路
            # _on_text/feed）下 brain 调用都不阻塞调用方
            asyncio.create_task(self.flush())

    async def flush(self) -> None:
        """将累积文本路由到 on_route 回调，然后清空 buffer。

        空白 buffer 是幂等 no-op（不触发路由）。
        """
        text = self._buffer.strip()
        self._buffer = ""
        if not text:
            return
        try:
            await self._on_route(text)
        except Exception as e:  # noqa: BLE001 - 路由失败不影响语音会话
            logger.warning("voice intent route failed: %s", e)

    def clear(self) -> None:
        """丢弃 buffer 不路由（barge-in 中断旧回复时使用）。"""
        self._buffer = ""
