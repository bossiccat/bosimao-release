"""意图→拆解管线单测（mock 本地 9B + DeepSeek + Injector）

覆盖：状态机流转 / 脱敏链路（摘要进 DeepSeek 前已脱敏）/ 熔断降级 /
确认令牌（未确认不注入）/ 过期 / 限频 / 锁并发 / 列表分页。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.brain.errors import BrainError
from app.brain.injector import InjectFocusResult, InjectResult
from app.brain.intent_service import IntentService
from app.brain.pipeline import BrainPipeline
from app.brain.store import TaskStore
from app.brain.task_service import TaskService
from app.config import BrainConfig

SECRET_PATH = "D:\\data\\secret\\app"


class FakeLocal:
    """本地 9B mock：摘要返回含路径原文（验证脱敏链路）；提取返回 JSON"""

    async def chat(self, prompt: str, max_tokens: int = 512) -> str:
        if "会话摘要器" in prompt:
            return f"用户需要重构 {SECRET_PATH} 目录的数据层，改成接口+实现，保留原有行为。"
        return (
            '{"intent_type": "refactor", "target_app": "codex", "confidence": 0.9,'
            ' "clarifying_questions": []}'
        )


class FakeDeepSeek:
    def __init__(self, key_ok: bool = True, circuit_open: bool = False) -> None:
        self._key_ok = key_ok
        self._circuit_open = circuit_open
        self.messages_sent: list[list[dict]] = []
        self.decompose_calls = 0

    def key_configured(self) -> bool:
        return self._key_ok

    def circuit_open(self) -> bool:
        return self._circuit_open

    async def chat(self, messages, *, max_tokens, temperature=0.2) -> str:
        self.messages_sent.append(messages)
        return (
            "请重构数据层：\n1. 拆分接口与实现\n2. 迁移现有调用\n3. 验证行为一致，失败回滚"
        )

    async def chat_json(self, messages, *, max_tokens, json_schema=None, temperature=0.2) -> dict:
        self.messages_sent.append(messages)
        self.decompose_calls += 1
        subs = []
        for i in range(1, 6):
            subs.append(
                {
                    "id": f"T{i}",
                    "goal": f"步骤{i}：完成拆解目标",
                    "acceptance": [f"验收点{i}"],
                    "rollback_hint": f"回滚提示{i}",
                    "depends_on": [f"T{i-1}"] if i > 1 else [],
                }
            )
        return {"subtasks": subs}


class FakeInjector:
    def __init__(self, focus_ok: bool = True, slow: bool = False) -> None:
        self.focus_ok = focus_ok
        self.slow = slow
        self.inject_calls = 0
        self.audit_entries: list[tuple[str, str]] = []
        self.fallback_files: list[str] = []

    async def validate_focus(self, target_app: str = "codex") -> InjectFocusResult:
        if self.focus_ok:
            return InjectFocusResult(ok=True, window_title="ChatGPT", reason="")
        return InjectFocusResult(ok=False, window_title="Calculator", reason="前台窗口标题不匹配 codex")

    async def inject(self, task) -> InjectResult:
        self.inject_calls += 1
        if self.slow:
            await asyncio.sleep(0.05)
        return InjectResult(ok=True, channel="sendinput")

    async def write_fallback_file(self, task) -> Path:
        self.fallback_files.append(task.task_id)
        return Path("/tmp") / f"{task.task_id}.md"

    def audit(self, task, action: str, result: str) -> None:
        self.audit_entries.append((action, result))


def make_pipeline(
    tmp_path: Path,
    key_ok: bool = True,
    circuit_open: bool = False,
    focus_ok: bool = True,
    slow_injector: bool = False,
) -> tuple[BrainPipeline, FakeDeepSeek, TaskStore, FakeInjector]:
    cfg = BrainConfig()
    deepseek = FakeDeepSeek(key_ok=key_ok, circuit_open=circuit_open)
    store = TaskStore(path=tmp_path / "brain_tasks.json")
    intent = IntentService(FakeLocal(), cfg)
    task_svc = TaskService(deepseek, cfg)
    injector = FakeInjector(focus_ok=focus_ok, slow=slow_injector)
    pipeline = BrainPipeline(cfg, deepseek, intent, task_svc, store, injector)
    return pipeline, deepseek, store, injector


class TestCreateIntent:
    @pytest.mark.asyncio
    async def test_create_intent_status_intent_ready(self, tmp_path):
        pipeline, _, store, _ = make_pipeline(tmp_path)
        task = await pipeline.create_intent("帮我重构项目数据层", target_app="codex")
        assert task.status == "intent_ready"
        assert task.task_id.startswith("BT-")
        assert task.intent.intent_type == "refactor"
        assert task.intent.confidence > 0.8
        assert store.get(task.task_id) is not None

    @pytest.mark.asyncio
    async def test_create_intent_no_key_50301(self, tmp_path):
        pipeline, _, _, _ = make_pipeline(tmp_path, key_ok=False)
        with pytest.raises(BrainError) as ei:
            await pipeline.create_intent("帮我重构")
        assert ei.value.code == 50301
        assert "DEEPSEEK_API_KEY" in ei.value.message

    @pytest.mark.asyncio
    async def test_create_intent_local_unavailable_degrades(self, tmp_path):
        """R3 降级：本地 9B 不可用 → 不再 50301，降级生成摘要并标记 degraded（防单点故障）"""
        from app.utils.metrics import metrics

        cfg = BrainConfig()
        deepseek = FakeDeepSeek()
        store = TaskStore(path=tmp_path / "b.json")
        intent = IntentService(None, cfg)  # 本地 9B 不可用
        pipeline = BrainPipeline(cfg, deepseek, intent, TaskService(deepseek, cfg), store, FakeInjector())
        before = metrics.degrade_total
        task = await pipeline.create_intent("帮我重构")
        assert task.status == "intent_ready"  # 路由 202 语义：不抛错、正常受理
        assert task.degraded is True
        assert task.intent.sanitized_summary  # 降级摘要非空
        assert metrics.degrade_total > before  # 降级次数已记录（可观测）

    @pytest.mark.asyncio
    async def test_summary_is_sanitized(self, tmp_path):
        pipeline, _, store, _ = make_pipeline(tmp_path)
        task = await pipeline.create_intent(f"重构 {SECRET_PATH} 的数据层")
        assert SECRET_PATH not in task.intent.sanitized_summary
        assert "[路径]" in task.intent.sanitized_summary


class TestDecompose:
    async def _ready_task(self, pipeline) -> str:
        task = await pipeline.create_intent("帮我重构项目数据层")
        return task.task_id

    @pytest.mark.asyncio
    async def test_decompose_to_awaiting_confirm(self, tmp_path):
        pipeline, deepseek, store, _ = make_pipeline(tmp_path)
        tid = await self._ready_task(pipeline)
        task = await pipeline.decompose_task(tid)
        assert task.status == "awaiting_confirm"
        assert 3 <= len(task.subtasks) <= 8
        assert task.instruction is not None
        assert task.instruction.generated_via == "deepseek"
        assert task.confirm_token is not None
        assert task.degraded is False
        assert deepseek.decompose_calls == 1

    @pytest.mark.asyncio
    async def test_sanitize_before_deepseek(self, tmp_path):
        pipeline, deepseek, _, _ = make_pipeline(tmp_path)
        task = await pipeline.create_intent(f"重构 {SECRET_PATH} 的数据层")
        await pipeline.decompose_task(task.task_id)
        all_msgs = "\n".join(str(m) for m in deepseek.messages_sent)
        assert SECRET_PATH not in all_msgs  # 进 DeepSeek 的输入已脱敏

    @pytest.mark.asyncio
    async def test_circuit_open_fallback_local(self, tmp_path):
        pipeline, deepseek, _, _ = make_pipeline(tmp_path, circuit_open=True)
        tid = await self._ready_task(pipeline)
        task = await pipeline.decompose_task(tid)
        assert task.degraded is True
        assert task.instruction.generated_via == "local_fallback"
        assert 3 <= len(task.subtasks) <= 8
        assert deepseek.decompose_calls == 0  # 熔断未调 DeepSeek

    @pytest.mark.asyncio
    async def test_decompose_unknown_task_40301(self, tmp_path):
        pipeline, _, _, _ = make_pipeline(tmp_path)
        with pytest.raises(BrainError) as ei:
            await pipeline.decompose_task("BT-nope")
        assert ei.value.code == 40301

    @pytest.mark.asyncio
    async def test_decompose_after_injected_40302(self, tmp_path):
        pipeline, _, store, _ = make_pipeline(tmp_path)
        tid = await self._ready_task(pipeline)
        task = await pipeline.decompose_task(tid)
        task.status = "injected"
        store.update(task)
        with pytest.raises(BrainError) as ei:
            await pipeline.decompose_task(tid)
        assert ei.value.code == 40302

    @pytest.mark.asyncio
    async def test_regenerate_rate_limit_42901(self, tmp_path):
        pipeline, _, _, _ = make_pipeline(tmp_path)
        tid = await self._ready_task(pipeline)
        await pipeline.decompose_task(tid)
        with pytest.raises(BrainError) as ei:
            await pipeline.decompose_task(tid, regenerate=True)
        assert ei.value.code == 42901


async def _awaiting(pipeline: BrainPipeline) -> tuple[str, str]:
    """创建意图并拆解到 awaiting_confirm，返回 (task_id, confirm_token)"""
    tid = (await pipeline.create_intent("帮我重构")).task_id
    task = await pipeline.decompose_task(tid)
    return tid, task.confirm_token


class TestConfirmInject:
    @pytest.mark.asyncio
    async def test_confirm_inject_success(self, tmp_path):
        pipeline, _, store, injector = make_pipeline(tmp_path)
        tid, token = await _awaiting(pipeline)
        result = await pipeline.confirm_inject(tid, "confirm", confirm_token=token)
        assert result["status"] == "injected"
        assert result["channel"] == "sendinput"
        assert injector.inject_calls == 1
        assert store.get(tid).status == "injected"
        assert ("inject", "ok") in injector.audit_entries

    @pytest.mark.asyncio
    async def test_confirm_without_token_rejected(self, tmp_path):
        pipeline, _, store, injector = make_pipeline(tmp_path)
        tid, _ = await _awaiting(pipeline)
        with pytest.raises(BrainError) as ei:
            await pipeline.confirm_inject(tid, "confirm", confirm_token=None)
        assert ei.value.code == 40302
        assert store.get(tid).status == "awaiting_confirm"  # 未确认不注入
        assert injector.inject_calls == 0

    @pytest.mark.asyncio
    async def test_confirm_wrong_token_rejected(self, tmp_path):
        pipeline, _, store, injector = make_pipeline(tmp_path)
        tid, _ = await _awaiting(pipeline)
        with pytest.raises(BrainError) as ei:
            await pipeline.confirm_inject(tid, "confirm", confirm_token="wrong-token")
        assert ei.value.code == 40302
        assert store.get(tid).status == "awaiting_confirm"
        assert injector.inject_calls == 0

    @pytest.mark.asyncio
    async def test_confirm_deny(self, tmp_path):
        pipeline, _, store, injector = make_pipeline(tmp_path)
        tid, token = await _awaiting(pipeline)
        result = await pipeline.confirm_inject(tid, "deny", confirm_token=token)
        assert result["status"] == "denied"
        assert store.get(tid).status == "denied"
        assert ("deny", "denied") in injector.audit_entries

    @pytest.mark.asyncio
    async def test_confirm_expired(self, tmp_path):
        pipeline, _, store, injector = make_pipeline(tmp_path)
        tid, token = await _awaiting(pipeline)
        task = store.get(tid)
        task.updated_at = time.time() - 1000  # 模拟超时
        store.update(task)
        with pytest.raises(BrainError) as ei:
            await pipeline.confirm_inject(tid, "confirm", confirm_token=token)
        assert ei.value.code == 40302
        assert store.get(tid).status == "expired"
        assert ("expire", "timeout") in injector.audit_entries

    @pytest.mark.asyncio
    async def test_confirm_focus_fail_40303(self, tmp_path):
        pipeline, _, store, injector = make_pipeline(tmp_path, focus_ok=False)
        tid, token = await _awaiting(pipeline)
        with pytest.raises(BrainError) as ei:
            await pipeline.confirm_inject(tid, "confirm", confirm_token=token)
        assert ei.value.code == 40303
        assert store.get(tid).status == "awaiting_confirm"
        assert injector.inject_calls == 0

    @pytest.mark.asyncio
    async def test_confirm_manual_fallback_channel(self, tmp_path):
        pipeline, _, store, _ = make_pipeline(tmp_path)
        tid, token = await _awaiting(pipeline)
        result = await pipeline.confirm_inject(tid, "confirm", confirm_token=token, manual=True)
        assert result["status"] == "injected"
        assert result["channel"] == "fallback"
        assert store.get(tid).status == "injected"

    @pytest.mark.asyncio
    async def test_inject_idempotent_after_injected(self, tmp_path):
        pipeline, _, _, injector = make_pipeline(tmp_path)
        tid, token = await _awaiting(pipeline)
        await pipeline.confirm_inject(tid, "confirm", confirm_token=token)
        result = await pipeline.confirm_inject(tid, "confirm", confirm_token=token)
        assert result["status"] == "injected"
        assert result["channel"] == "none"
        assert injector.inject_calls == 1  # 幂等不重复注入


class TestLock:
    @pytest.mark.asyncio
    async def test_lock_prevents_inject_instruct_concurrency(self, tmp_path):
        pipeline, _, store, _ = make_pipeline(tmp_path, slow_injector=True)
        tid, token = await _awaiting(pipeline)

        t1 = asyncio.create_task(pipeline.confirm_inject(tid, "confirm", confirm_token=token))
        await asyncio.sleep(0.01)  # 让 inject 先拿锁并进入慢注入
        t2 = asyncio.create_task(pipeline.decompose_task(tid, regenerate=True))

        r1, r2 = await asyncio.gather(t1, t2, return_exceptions=True)
        assert r1["status"] == "injected"
        # 重生成被锁阻塞，注入完成后看到 injected → 40302，不覆盖状态
        assert isinstance(r2, BrainError) and r2.code == 40302
        assert store.get(tid).status == "injected"


class TestList:
    @pytest.mark.asyncio
    async def test_list_tasks_pagination(self, tmp_path):
        pipeline, _, _, _ = make_pipeline(tmp_path)
        for _ in range(3):
            await pipeline.create_intent("帮我做一件事")
        res = pipeline.list_tasks(page=1, limit=2)
        assert res["total"] == 3
        assert len(res["items"]) == 2
        assert res["hasMore"] is True
        res2 = pipeline.list_tasks(page=2, limit=2)
        assert len(res2["items"]) == 1
        assert res2["hasMore"] is False

    @pytest.mark.asyncio
    async def test_list_tasks_filter_status(self, tmp_path):
        pipeline, _, _, _ = make_pipeline(tmp_path)
        await pipeline.create_intent("帮我做一件事")
        res = pipeline.list_tasks(status="intent_ready")
        assert res["total"] == 1
        res2 = pipeline.list_tasks(status="injected")
        assert res2["total"] == 0
