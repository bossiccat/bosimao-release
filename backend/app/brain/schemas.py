"""大脑 Pydantic 模型（backend-brain-spec §5.2 JSON Schema 落地）

IntentInput / IntentExtract / Subtask / SubtaskList / InstructionDraft /
ReviewVerdict / BrainTask —— 请求体校验 + 内部任务模型 + 响应组装共用。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal[
    "intent_ready", "decomposed", "awaiting_confirm", "injected", "denied", "failed", "expired"
]


class IntentInput(BaseModel):
    """POST /api/v1/brain/intent 请求体"""

    text: str = Field(min_length=2, max_length=2000, description="用户自然语言意图")
    source: Literal["text", "voice"] = "text"
    target_app: str | None = Field(
        default=None, description="可选，缺省由本地 9B 判定（codex/trae/hermes/workbuddy）"
    )
    session_id: str | None = None


class IntentExtract(BaseModel):
    """本地 9B 意图理解产物（R2）"""

    intent_type: str = "other"
    target_app: str = "codex"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    clarifying_questions: list[str] = Field(default_factory=list)
    sanitized_summary: str = Field(default="", max_length=1200)


class Subtask(BaseModel):
    """单步可执行子任务"""

    id: str = Field(description="子任务 id，如 T1")
    goal: str = Field(description="单步目标（对 Codex 可执行）")
    acceptance: list[str] = Field(default_factory=list, description="验收点（≥1）")
    rollback_hint: str = Field(default="", description="回滚提示")
    depends_on: list[str] = Field(default_factory=list, description="依赖前置子任务 id（空=无依赖）")


class SubtaskList(BaseModel):
    task_id: str
    subtasks: list[Subtask] = Field(min_length=3, max_length=8)


class InstructionDraft(BaseModel):
    """对 Codex 最优指令（注入/粘贴内容）"""

    task_id: str
    instruction_text: str = Field(max_length=6000)
    preview: str = Field(default="", description="展示用预览（脱敏，≤200 字）")
    generated_via: Literal["deepseek", "local_fallback"] = "deepseek"


class ReviewVerdict(BaseModel):
    verdict: Literal["on_track", "off_track"]
    evidence: str = ""
    correction_suggestion: str | None = None


class BrainTask(BaseModel):
    """任务模型（内存 + JSON 持久化，store.py）"""

    task_id: str
    status: TaskStatus
    intent: IntentExtract | None = None
    subtasks: list[Subtask] | None = None
    instruction: InstructionDraft | None = None
    review: ReviewVerdict | None = None
    created_at: float
    updated_at: float
    error: str | None = None
    degraded: bool = False
    source: str = "text"
    session_id: str | None = None
    # O-012/O-013：注入前确认令牌（仅内部校验，不进预览/审计）
    confirm_token: str | None = None
