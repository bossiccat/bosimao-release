"""意图→拆解管线门面（backend-brain-spec §5，状态机唯一编排点）

状态机：
  intent_ready ──decompose──► awaiting_confirm ──confirm──► injected
                                        ├──deny──► denied
                                        └──timeout(300s)► expired
  失败 → failed（error 记录原因，可重试）

约束：
- task 级 asyncio.Lock 覆盖 inject+instruct（防先注入后生成，spec §11.6）
- 重生成限频 ≤1 次/min → 42901（spec C-3）
- 注入前校验 confirm_token（O-013 受控注入）
- DeepSeek 熔断/失败 → 本地简化降级 + degraded 标记（R4/R5）
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import secrets
import time
from typing import Any

from ..core.events import (
    EVT_BRAIN_DEGRADED,
    EVT_BRAIN_INJECT,
    EVT_BRAIN_INTENT,
    EVT_BRAIN_TASK,
    EventBus,
)
from ..utils.metrics import metrics
from .deepseek_client import DeepSeekClient
from .errors import (
    focus_failed,
    inject_in_progress,
    regenerate_limited,
    service_unready,
    status_not_allowed,
    task_not_found,
)
from .injector import InjectFocusResult, Injector
from .intent_service import IntentService
from .router import R4_DECOMPOSE, degrade_event, route
from .sanitizer import sanitize, truncate_head_tail
from .schemas import BrainTask, IntentExtract
from .store import TaskStore
from .task_service import TaskService

logger = logging.getLogger(__name__)


class BrainPipeline:
    def __init__(
        self,
        cfg: Any,
        deepseek: DeepSeekClient,
        intent: IntentService,
        task_svc: TaskService,
        store: TaskStore,
        injector: Injector,
        bus: EventBus | None = None,
    ) -> None:
        self._cfg = cfg
        self._deepseek = deepseek
        self._intent = intent
        self._task_svc = task_svc
        self._store = store
        self._injector = injector
        self._bus = bus
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_instruct_at: dict[str, float] = {}
        self._injecting: set[str] = set()
        self._seq = itertools.count(1)

    # ---------- 锁 ----------
    def _lock(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        return self._locks[task_id]

    def _gen_task_id(self) -> str:
        date = time.strftime("%Y%m%d")
        return f"BT-{date}-{next(self._seq):03d}"

    # ---------- 1. 意图受理（本地提取 + 脱敏摘要） ----------
    async def create_intent(
        self,
        text: str,
        source: str = "text",
        target_app: str | None = None,
        session_id: str | None = None,
    ) -> BrainTask:
        if not self._deepseek.key_configured():
            raise service_unready(
                "DEEPSEEK_API_KEY 未配置，大脑拆解/指令生成不可用（联调待 key），请配置 .env 后重启"
            )
        extract: IntentExtract = await self._intent.extract(text, target_app)
        degraded = False
        try:
            summary = await self._intent.build_summary(text)  # R3 本地 9B 摘要（隐私第一）
        except Exception as e:  # noqa: BLE001 - 本地 9B 超时/失败 → 降级（防单点故障，spec R3 扩展）
            logger.warning("本地 9B 摘要不可用，降级生成摘要: %s", e)
            degraded = True
            summary = await self._degraded_summary(text)
            if self._bus:
                await self._bus.emit(
                    EVT_BRAIN_DEGRADED,
                    {"task_id": "", "stage": "intent_summary", **degrade_event(route(R4_DECOMPOSE, self._deepseek))},
                )
        extract.sanitized_summary = summary
        now = time.time()
        task = BrainTask(
            task_id=self._gen_task_id(),
            status="intent_ready",
            intent=extract,
            created_at=now,
            updated_at=now,
            source=source,
            session_id=session_id,
            degraded=degraded,
        )
        self._store.create(task)
        await self._emit(
            EVT_BRAIN_INTENT,
            {
                "task_id": task.task_id,
                "status": task.status,
                "confidence": extract.confidence,
                "clarifying_questions": extract.clarifying_questions,
            },
        )
        logger.info("意图已受理: task=%s type=%s conf=%.2f", task.task_id, extract.intent_type, extract.confidence)
        return task

    # ---------- 2. 拆解 + 指令生成 → awaiting_confirm ----------
    async def decompose_task(self, task_id: str, regenerate: bool = False) -> BrainTask:
        async with self._lock(task_id):
            task = self._store.get(task_id)
            if task is None:
                raise task_not_found(f"task_id 不存在: {task_id}")
            if task.status == "awaiting_confirm" and not regenerate:
                return task  # 幂等返回当前任务
            if task.status not in ("intent_ready", "decomposed", "awaiting_confirm", "failed"):
                raise status_not_allowed(f"任务状态不允许拆解（当前 {task.status}）")
            if regenerate:
                last = self._last_instruct_at.get(task_id, 0.0)
                if time.time() - last < self._cfg.inject.regenerate_limit_seconds:
                    raise regenerate_limited()

            decision = route(R4_DECOMPOSE, self._deepseek)
            degraded = decision.fallback
            try:
                if decision.engine == "deepseek":
                    subtasks = await self._task_svc.decompose(task)
                    instruction = await self._task_svc.instruct(task, subtasks)
                else:
                    subtasks = self._task_svc.local_decompose(task)
                    instruction = self._task_svc.local_instruct(task, subtasks)
            except Exception as e:  # noqa: BLE001 - DeepSeek 失败 → 本地降级 + 明示（R4/R5）
                logger.warning("拆解/指令失败，本地降级: task=%s err=%s", task_id, e)
                degraded = True
                subtasks = self._task_svc.local_decompose(task)
                instruction = self._task_svc.local_instruct(task, subtasks)
                if self._bus:
                    await self._bus.emit(
                        EVT_BRAIN_DEGRADED,
                        {"task_id": task_id, **degrade_event(decision)},
                    )

            task.subtasks = subtasks
            task.instruction = instruction
            task.status = "awaiting_confirm"
            # 保留任何阶段的降级标记（如 intent 摘要已降级 → 任务仍标记 degraded，可观测）
            task.degraded = task.degraded or degraded
            task.error = None
            task.confirm_token = secrets.token_urlsafe(16)
            task.updated_at = time.time()
            self._last_instruct_at[task_id] = task.updated_at
            self._store.update(task)
            await self._emit(
                EVT_BRAIN_TASK,
                {
                    "task_id": task_id,
                    "status": task.status,
                    "preview": instruction.preview,
                    "degraded": degraded,
                    "generated_via": instruction.generated_via,
                },
            )
            logger.info("拆解完成: task=%s steps=%d via=%s degraded=%s", task_id, len(subtasks), instruction.generated_via, degraded)
            return task

    # ---------- 3. 注入确认（O-012/O-013） ----------
    async def confirm_inject(
        self,
        task_id: str,
        decision: str,
        confirm_token: str | None = None,
        manual: bool = False,
    ) -> dict[str, Any]:
        async with self._lock(task_id):
            task = self._store.get(task_id)
            if task is None:
                raise task_not_found(f"task_id 不存在: {task_id}")
            if task.status == "injected":
                return {"task_id": task_id, "status": "injected", "channel": "none"}  # 幂等
            if task_id in self._injecting:
                raise inject_in_progress()
            if task.status != "awaiting_confirm":
                raise status_not_allowed(f"任务状态不允许注入（当前 {task.status}）")

            # 过期检查（awaiting_confirm 起 300s）
            if time.time() - task.updated_at > self._cfg.inject.expire_seconds:
                task.status = "expired"
                task.updated_at = time.time()
                self._store.update(task)
                self._injector.audit(task, "expire", "timeout")
                raise status_not_allowed("指令已过期（未及时确认），请重新生成")

            if decision == "deny":
                task.status = "denied"
                task.updated_at = time.time()
                self._store.update(task)
                self._injector.audit(task, "deny", "denied")
                await self._emit(EVT_BRAIN_INJECT, {"task_id": task_id, "status": "denied"})
                return {"task_id": task_id, "status": "denied", "channel": "none"}

            # decision == "confirm"：先验确认令牌（未确认不注入）
            if not confirm_token or confirm_token != task.confirm_token:
                raise status_not_allowed("确认令牌无效，请先获取拆解结果再确认")

            if manual:
                task.status = "injected"
                task.updated_at = time.time()
                self._store.update(task)
                self._injector.audit(task, "fallback", "ok")
                await self._emit(EVT_BRAIN_INJECT, {"task_id": task_id, "status": "injected", "channel": "fallback"})
                return {"task_id": task_id, "status": "injected", "channel": "fallback"}

            self._injecting.add(task_id)
            try:
                focus: InjectFocusResult = await self._injector.validate_focus(self._cfg.inject.target_app)
                if not focus.ok:
                    raise focus_failed(focus.reason)
                result = await self._injector.inject(task)
                task.status = "injected"
                task.updated_at = time.time()
                self._store.update(task)
                self._injector.audit(task, "inject", "ok" if result.ok else "fail")
                await self._emit(
                    EVT_BRAIN_INJECT,
                    {"task_id": task_id, "status": "injected", "channel": result.channel},
                )
                return {"task_id": task_id, "status": "injected", "channel": result.channel}
            finally:
                self._injecting.discard(task_id)

    # ---------- 4. 任务列表 ----------
    def list_tasks(self, status: str | None = None, page: int = 1, limit: int = 20) -> dict[str, Any]:
        items, total = self._store.list(status=status, page=page, limit=limit)
        return {
            "items": [t.model_dump(mode="json") for t in items],
            "total": total,
            "page": page,
            "limit": limit,
            "hasMore": page * limit < total,
        }

    # ---------- 内部 ----------
    async def _degraded_summary(self, text: str) -> str:
        """R3 降级摘要（防单点故障）：优先 DeepSeek 生成（输入先脱敏），失败则用脱敏原文兜底。

        可观测：metrics.record_degrade 记录降级次数与原因；任务 degraded 标记随响应返回前端。
        """
        try:
            if self._deepseek.key_configured() and not self._deepseek.circuit_open():
                system = (
                    "你是会话摘要器。把以下用户意图压缩为 ≤200 字摘要，"
                    "剔除文件路径、代码、密钥、联系方式等敏感信息，只输出摘要正文："
                )
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": sanitize(text)},
                ]
                raw = await self._deepseek.chat(
                    messages, max_tokens=self._cfg.intent.local_max_tokens
                )
                clean = sanitize(raw).strip()
                if clean:
                    metrics.record_degrade("summary_deepseek")
                    return truncate_head_tail(clean, self._cfg.intent.summary_max_chars)
        except Exception as e:  # noqa: BLE001 - DeepSeek 也不可用 → 原文兜底
            logger.warning("DeepSeek 降级摘要失败，回退原文: %s", e)
        metrics.record_degrade("summary_passthrough")
        return truncate_head_tail(sanitize(text), self._cfg.intent.summary_max_chars)

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._bus is not None:
            await self._bus.emit(event, data)
