"""P0-3：/intent extract 与 build_summary 并行化（asyncio.gather）

根因（internal-latency-budget §1.4 S2 / P0-3）：create_intent 串行跑两轮
本地 9B（extract → build_summary），二者无数据依赖 → 5~8s 常态，
rtc_bridge 侧 5s urlopen 超时必现。并行后总耗时 ≈ max(两轮) 而非 sum。
异常语义保持：summary 失败 → 降级（DeepSeek → 脱敏原文），intent 仍受理。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.brain.intent_service import IntentService
from app.brain.pipeline import BrainPipeline
from app.brain.store import TaskStore
from app.brain.task_service import TaskService
from app.config import BrainConfig


class SlowLocal:
    """本地 9B mock：每轮 chat 挂 0.3s（模拟真实推理耗时）"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, prompt: str, max_tokens: int = 512) -> str:
        self.calls.append(prompt[:20])
        await asyncio.sleep(0.3)
        if "会话摘要器" in prompt:
            return "用户意图摘要：重构 config 模块。"
        return (
            '{"intent_type": "refactor", "target_app": "codex", "confidence": 0.9,'
            ' "clarifying_questions": []}'
        )


class DeepSeekOk:
    def key_configured(self) -> bool:
        return True

    def circuit_open(self) -> bool:
        return False

    async def chat(self, messages, *, max_tokens, temperature=0.2) -> str:
        return "用户意图摘要：重构 config 模块。"

    async def chat_json(self, messages, *, max_tokens, json_schema=None, temperature=0.2) -> dict:
        return {"subtasks": []}


class FakeInjector:
    async def validate_focus(self, target_app: str = "codex"):
        return None

    async def inject(self, task):
        return None

    async def write_fallback_file(self, task):
        return None

    def audit(self, task, action: str, result: str) -> None:
        pass


def _make_pipeline(local) -> BrainPipeline:
    cfg = BrainConfig()
    deepseek = DeepSeekOk()
    return BrainPipeline(
        cfg, deepseek, IntentService(local, cfg), TaskService(deepseek, cfg),
        TaskStore(path=None), FakeInjector(),
    )


@pytest.mark.asyncio
async def test_extract_and_summary_run_in_parallel(tmp_path):
    """extract 与 summary 无数据依赖 → 并行，总耗时 ≈ max 而非 sum"""
    local = SlowLocal()
    pipeline = _make_pipeline(local)
    t0 = time.monotonic()
    task = await pipeline.create_intent("帮我重构 config 模块")
    elapsed = time.monotonic() - t0
    assert task.status == "intent_ready"
    assert task.degraded is False
    assert "config" in task.intent.sanitized_summary
    assert len(local.calls) == 2, "extract 与 summary 各调用一次本地 9B"
    # 串行基线 ≈ 0.6s+；并行后应 ≈ 0.3s（留余量 0.5s 判串行/并行）
    assert elapsed < 0.5, (
        f"extract 与 summary 应并行执行（总耗时 ≈ max 而非 sum），实际 {elapsed:.3f}s"
    )
