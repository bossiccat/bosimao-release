"""意图受理 R3 降级单测（防单点故障：本地 9B 超时/失败 → 降级不阻塞）

- mock 本地调用抛超时 → 断言降级路径生效：intent 仍正常受理（路由 202 语义）
- 降级摘要来源：DeepSeek 优先，失败回退脱敏原文
- 可观测：metrics.record_degrade 计数 + task.degraded 标记（响应透出前端）
"""
from __future__ import annotations

import asyncio

import pytest

from app.brain.intent_service import IntentService
from app.brain.pipeline import BrainPipeline
from app.brain.store import TaskStore
from app.brain.task_service import TaskService
from app.config import BrainConfig
from app.utils.metrics import metrics


class LocalTimeout:
    """本地 9B mock：意图提取正常，但摘要调用抛超时（模拟本地引擎挂起/超时）"""

    async def chat(self, prompt: str, max_tokens: int = 512) -> str:
        if "会话摘要器" in prompt:
            raise asyncio.TimeoutError("local 9B decode 超时（>60s）")
        return (
            '{"intent_type": "refactor", "target_app": "codex", "confidence": 0.9,'
            ' "clarifying_questions": []}'
        )


class DeepSeekOk:
    """DeepSeek mock：key 可用、熔断关闭；chat 返回摘要文本，chat_json 返回拆解"""

    def key_configured(self) -> bool:
        return True

    def circuit_open(self) -> bool:
        return False

    async def chat(self, messages, *, max_tokens, temperature=0.2) -> str:
        return "用户意图摘要：重构 config 模块，把 yaml 校验抽成独立 service 并补单测。"

    async def chat_json(self, messages, *, max_tokens, json_schema=None, temperature=0.2) -> dict:
        subs = [
            {
                "id": f"T{i}",
                "goal": f"步骤{i}：完成重构目标",
                "acceptance": [f"验收点{i}"],
                "rollback_hint": f"回滚提示{i}",
                "depends_on": [f"T{i-1}"] if i > 1 else [],
            }
            for i in range(1, 5)
        ]
        return {"subtasks": subs}


class DeepSeekDown:
    """DeepSeek 也不可用：key 已配但熔断开启 → 降级摘要回退脱敏原文"""

    def key_configured(self) -> bool:
        return True

    def circuit_open(self) -> bool:
        return True  # 熔断开启 → 跳过 DeepSeek，回退脱敏原文

    async def chat(self, messages, *, max_tokens, temperature=0.2) -> str:
        raise AssertionError("熔断开启不应调用 DeepSeek")

    async def chat_json(self, messages, *, max_tokens, json_schema=None, temperature=0.2) -> dict:
        raise AssertionError("熔断开启不应调用 DeepSeek")


class FakeInjector:
    async def validate_focus(self, target_app: str = "codex"):
        return None

    async def inject(self, task):
        return None

    async def write_fallback_file(self, task):
        return None

    def audit(self, task, action: str, result: str) -> None:
        pass


class TestIntentDegrade:
    @pytest.mark.asyncio
    async def test_local_timeout_degrades_to_deepseek_summary(self, tmp_path):
        """本地 9B 摘要超时 → DeepSeek 生成摘要，intent 仍受理（路由 202）"""
        from app.brain.pipeline import BrainPipeline

        cfg = BrainConfig()
        deepseek = DeepSeekOk()
        intent = IntentService(LocalTimeout(), cfg)
        pipeline = BrainPipeline(
            cfg, deepseek, intent, TaskService(deepseek, cfg),
            TaskStore(path=tmp_path / "d1.json"), FakeInjector(),
        )
        before = metrics.degrade_total
        task = await pipeline.create_intent("帮我重构 config 模块")
        assert task.status == "intent_ready"  # 不抛 50301 → 路由层 202
        assert task.degraded is True
        assert "config" in task.intent.sanitized_summary  # DeepSeek 摘要已落地
        assert metrics.degrade_total == before + 1
        assert metrics.degrade_reasons.get("summary_deepseek", 0) >= 1

    @pytest.mark.asyncio
    async def test_local_and_deepseek_down_falls_back_to_original(self, tmp_path):
        """本地 9B 与 DeepSeek 都不可用 → 回退脱敏原文，仍受理并标记 degraded"""
        from app.brain.pipeline import BrainPipeline

        cfg = BrainConfig()
        deepseek = DeepSeekDown()
        intent = IntentService(LocalTimeout(), cfg)
        pipeline = BrainPipeline(
            cfg, deepseek, intent, TaskService(deepseek, cfg),
            TaskStore(path=tmp_path / "d2.json"), FakeInjector(),
        )
        before = metrics.degrade_total
        task = await pipeline.create_intent("帮我重构 config 模块")
        assert task.status == "intent_ready"
        assert task.degraded is True
        assert "config" in task.intent.sanitized_summary  # 脱敏原文兜底
        assert metrics.degrade_total == before + 1
        assert metrics.degrade_reasons.get("summary_passthrough", 0) >= 1

    @pytest.mark.asyncio
    async def test_degraded_intent_still_decomposes_to_awaiting_confirm(self, tmp_path):
        """降级受理后仍可走拆解管线 → awaiting_confirm（状态机不因降级断链）"""
        from app.brain.pipeline import BrainPipeline

        cfg = BrainConfig()
        deepseek = DeepSeekOk()
        intent = IntentService(LocalTimeout(), cfg)
        pipeline = BrainPipeline(
            cfg, deepseek, intent, TaskService(deepseek, cfg),
            TaskStore(path=tmp_path / "d3.json"), FakeInjector(),
        )
        task = await pipeline.create_intent("帮我重构 config 模块")
        assert task.degraded is True
        done = await pipeline.decompose_task(task.task_id)
        assert done.status == "awaiting_confirm"
        assert 3 <= len(done.subtasks) <= 8
        assert done.instruction is not None
        assert done.instruction.generated_via == "deepseek"
        assert done.confirm_token is not None
