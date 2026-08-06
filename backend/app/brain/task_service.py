"""任务拆解 + 指令生成（DeepSeek，R4/R5）+ 本地降级

- decompose：DeepSeek chat_json → SubtaskList(3-8 步)，输入已脱敏
- instruct：DeepSeek chat → InstructionDraft（注入 Codex 的指令文本 + 预览）
- local_decompose / local_instruct：熔断/失败时本地简化降级（≤3 步）+ 明示降级
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from ..config import BrainConfig
from .prompts import DECOMPOSE_FALLBACK, INSTRUCT_FALLBACK, load_prompt
from .sanitizer import sanitize
from .schemas import BrainTask, InstructionDraft, Subtask

logger = logging.getLogger(__name__)

# 拆解 JSON schema（prompt 约束；V4 Flash 走 response_format=json_object）
DECOMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "goal", "acceptance", "rollback_hint", "depends_on"],
                "properties": {
                    "id": {"type": "string"},
                    "goal": {"type": "string"},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                    "rollback_hint": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
    "required": ["subtasks"],
}

_VALID_INTENTS = (
    "refactor", "implement", "fix_bug", "add_feature", "optimize", "test", "explain", "other"
)


class DeepSeekChat(Protocol):
    async def chat(self, messages: list[dict], *, max_tokens: int, temperature: float = 0.2) -> str: ...

    async def chat_json(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        json_schema: dict | None = None,
        temperature: float = 0.2,
    ) -> dict: ...


class TaskService:
    def __init__(self, deepseek: DeepSeekChat, cfg: BrainConfig) -> None:
        self._deepseek = deepseek
        self._cfg = cfg

    # ---------- R4：拆解（DeepSeek） ----------
    async def decompose(self, task: BrainTask) -> list[Subtask]:
        summary = sanitize(task.intent.sanitized_summary if task.intent else "")
        intent_type = task.intent.intent_type if task.intent else "other"
        system = load_prompt("config/prompts/decompose.md", DECOMPOSE_FALLBACK)
        user = (
            f"用户意图类型：{intent_type}\n脱敏摘要：{summary}\n\n"
            f"请输出 JSON（匹配 schema）:\n{self._schema_text()}"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        data = await self._deepseek.chat_json(
            messages,
            max_tokens=self._cfg.deepseek.max_tokens_decompose,
            json_schema=DECOMPOSE_SCHEMA,
        )
        raw = data.get("subtasks") or []
        if not isinstance(raw, list) or not (3 <= len(raw) <= 8):
            logger.warning("DeepSeek 拆解数量异常（%d），截断到 3-8", len(raw) if isinstance(raw, list) else -1)
            raw = raw[:8] if isinstance(raw, list) else []
        subtasks: list[Subtask] = []
        for i, item in enumerate(raw[:8], start=1):
            if not isinstance(item, dict):
                continue
            subtasks.append(
                Subtask(
                    id=str(item.get("id") or f"T{i}"),
                    goal=str(item.get("goal") or "（缺目标）"),
                    acceptance=[str(a) for a in (item.get("acceptance") or [])],
                    rollback_hint=str(item.get("rollback_hint") or ""),
                    depends_on=[str(d) for d in (item.get("depends_on") or [])],
                )
            )
        if len(subtasks) < 3:
            # 兜底：不足 3 步直接补足（保证 SubtaskList minItems=3）
            while len(subtasks) < 3:
                n = len(subtasks) + 1
                subtasks.append(
                    Subtask(
                        id=f"T{n}",
                        goal=f"（补充）完成剩余工作并自检，验收标准见上下文",
                        acceptance=["任务完成且可验证"],
                        rollback_hint="",
                        depends_on=[s.id for s in subtasks],
                    )
                )
        return subtasks[:8]

    # ---------- R5：指令生成（DeepSeek） ----------
    async def instruct(self, task: BrainTask, subtasks: list[Subtask]) -> InstructionDraft:
        system = load_prompt("config/prompts/instruct_codex.md", INSTRUCT_FALLBACK)
        payload = [s.model_dump(mode="json") for s in subtasks]
        user = f"子任务清单 JSON：\n{payload}\n\n请生成一段可直接粘贴进 Codex 的中文指令。"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = await self._deepseek.chat(
            messages, max_tokens=self._cfg.deepseek.max_tokens_instruct
        )
        text = text.strip()
        if not text:
            raise RuntimeError("DeepSeek 指令生成为空")
        return InstructionDraft(
            task_id=task.task_id,
            instruction_text=text,
            preview=sanitize(text)[:200],
            generated_via="deepseek",
        )

    # ---------- 本地降级（R4/R5 fallback） ----------
    def local_decompose(self, task: BrainTask) -> list[Subtask]:
        intent = task.intent
        raw_summary = intent.sanitized_summary if intent else ""
        summary = sanitize(raw_summary)[:120]
        itype = intent.intent_type if intent else "other"
        steps: list[tuple[str, str, list[str]]] = [
            ("梳理并理解需求", f"阅读并梳理需求摘要「{summary}」，明确目标与约束", ["已理解需求要点，无歧义"]),
            (f"按目标实施（{itype}）", f"依据目标完成主体工作，分小步推进并验证", ["主体工作完成，可运行/可验证"]),
            ("验收与回滚检查", "对照验收点自检；发现问题回滚到上一步并重试", ["验收点全部通过，无遗留错误"]),
        ]
        subtasks: list[Subtask] = []
        for i, (goal, detail, acc) in enumerate(steps, start=1):
            subtasks.append(
                Subtask(
                    id=f"T{i}",
                    goal=f"{goal}：{detail}"[:200],
                    acceptance=acc,
                    rollback_hint=f"回滚到 T{i-1} 状态后重试" if i > 1 else "保留原始代码备份，失败即还原",
                    depends_on=[f"T{i-1}"] if i > 1 else [],
                )
            )
        return subtasks

    def local_instruct(self, task: BrainTask, subtasks: list[Subtask]) -> InstructionDraft:
        lines = [f"{i}. {s.goal}" for i, s in enumerate(subtasks, start=1)]
        text = (
            "请按以下步骤执行（本地简化降级指令）：\n"
            + "\n".join(lines)
            + "\n\n约束：分步推进，每步自检；失败按回滚提示回退；全部完成后按验收点逐项核对。"
        )
        return InstructionDraft(
            task_id=task.task_id,
            instruction_text=text,
            preview=sanitize(text)[:200],
            generated_via="local_fallback",
        )

    @staticmethod
    def _schema_text() -> str:
        import json

        return json.dumps(DECOMPOSE_SCHEMA, ensure_ascii=False)
