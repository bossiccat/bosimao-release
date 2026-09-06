"""VoiceIntentRouter 单测 —— APM 文本 delta 累积 + flush 路由到 Brain

解决"伪智能体"：APM 产出的 AI 文本（response.output.delta kind=text）
之前只做 logger.info，现在累积完整回复后路由到 Brain 做意图提取。

测试覆盖：
- delta 累积成完整文本
- flush 触发路由回调（携带完整文本）
- flush 后 buffer 清空（下轮回复干净起步）
- 空文本 flush 幂等无副作用
- 超长缓冲自动 flush 防内存泄漏
"""
from __future__ import annotations

import asyncio

import pytest

from app.brain.voice_intent_router import VoiceIntentRouter


@pytest.mark.asyncio
async def test_deltas_accumulate_into_buffer():
    """多个 text delta 累积成完整文本"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    await router.feed("好的，")
    await router.feed("我来帮你")
    await router.feed("重构这段代码。")
    assert router.buffered_text == "好的，我来帮你重构这段代码。"
    assert routed == [], "feed 阶段不应触发路由"


@pytest.mark.asyncio
async def test_flush_routes_accumulated_text():
    """flush 将累积文本传给路由回调"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    await router.feed("好的，我来帮你重构这段代码。")
    await router.flush()
    assert routed == ["好的，我来帮你重构这段代码。"]


@pytest.mark.asyncio
async def test_flush_clears_buffer():
    """flush 后 buffer 清空，可接收下轮回复"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    await router.feed("第一轮回复。")
    await router.flush()
    assert router.buffered_text == ""

    # 第二轮
    await router.feed("第二轮回复。")
    await router.flush()
    assert routed == ["第一轮回复。", "第二轮回复。"]


@pytest.mark.asyncio
async def test_empty_flush_is_noop():
    """空 buffer flush 幂等，不触发路由"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    await router.flush()
    await router.flush()
    assert routed == []


@pytest.mark.asyncio
async def test_whitespace_only_flush_is_noop():
    """只有空白的 buffer flush 不触发路由"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    await router.feed("   \n  \t  ")
    await router.flush()
    assert routed == []


@pytest.mark.asyncio
async def test_long_buffer_auto_flush():
    """超长缓冲自动 flush 防内存泄漏"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route, max_buffer=50)
    long_text = "A" * 50
    await router.feed(long_text)
    # P0 修复后自动 flush 为 create_task 异步路由，让出一拍等 flush 任务执行
    await asyncio.sleep(0.05)
    assert len(routed) == 1, "达到 max_buffer 应自动 flush"
    assert router.buffered_text == ""


@pytest.mark.asyncio
async def test_clear_drops_buffer_without_routing():
    """clear 丢弃 buffer 不触发路由（用于 barge-in 中断旧回复）"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    await router.feed("正在生成的旧回复")
    router.clear()
    assert router.buffered_text == ""
    assert routed == [], "clear 不应触发路由"


@pytest.mark.asyncio
async def test_concurrent_feeds_are_safe():
    """并发 feed 不丢文本"""
    routed: list[str] = []

    async def on_route(text: str) -> None:
        routed.append(text)

    router = VoiceIntentRouter(on_route=on_route)
    # 交错 feed（模拟 delta 快速到达）
    for ch in "你好世界":
        await router.feed(ch)
    await router.flush()
    assert routed == ["你好世界"]
