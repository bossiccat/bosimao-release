"""API 路由：大脑闭环（backend-brain-spec §9，V1.5）

- POST /api/v1/brain/intent   意图输入（本地提取+脱敏摘要+建任务草稿）→ 202+task_id
- POST /api/v1/brain/task     拆解+指令生成（DeepSeek）→ awaiting_confirm
- POST /api/v1/brain/inject   注入确认 {task_id, decision, confirm_token}（O-013）
- GET  /api/v1/brain/tasks    任务列表（分页/状态过滤）

只编排不写业务：参数校验（Pydantic）→ 调 pipeline → 组装响应；错误码按 spec §9.2。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..brain.errors import BrainError
from ..brain.pipeline import BrainPipeline

router = APIRouter(prefix="/api/v1/brain", tags=["brain"])

# 由 main 注入（避免循环依赖，与 routes_capture 同模式）
pipeline: BrainPipeline | None = None


def _raise(e: BrainError) -> None:
    raise HTTPException(status_code=e.status_code, detail=e.to_dict())


class IntentRequest(BaseModel):
    text: str = Field(min_length=2, max_length=2000)
    source: str = Field(default="text", pattern="^(text|voice)$")
    target_app: str | None = None
    session_id: str | None = None


class TaskRequest(BaseModel):
    task_id: str
    regenerate: bool = False


class InjectRequest(BaseModel):
    task_id: str
    decision: str = Field(pattern="^(confirm|deny)$")
    confirm_token: str | None = None
    manual: bool = False


@router.post("/intent", status_code=202)
async def create_brain_intent(req: IntentRequest) -> dict:
    """意图输入：本地 9B 提取 → 脱敏摘要 → 建任务草稿（status=intent_ready）"""
    if pipeline is None:
        raise HTTPException(503, {"code": 50301, "message": "brain pipeline 未初始化"})
    try:
        task = await pipeline.create_intent(
            text=req.text,
            source=req.source,
            target_app=req.target_app,
            session_id=req.session_id,
        )
    except BrainError as e:
        _raise(e)
    return {"code": 0, "data": task.model_dump(mode="json"), "message": "意图已受理"}


@router.post("/task")
async def decompose_brain_task(req: TaskRequest) -> dict:
    """拆解 + 指令生成 → awaiting_confirm（含 instruction.preview + confirm_token）"""
    if pipeline is None:
        raise HTTPException(503, {"code": 50301, "message": "brain pipeline 未初始化"})
    try:
        task = await pipeline.decompose_task(req.task_id, regenerate=req.regenerate)
    except BrainError as e:
        _raise(e)
    return {"code": 0, "data": task.model_dump(mode="json"), "message": "拆解完成"}


@router.post("/inject")
async def confirm_brain_inject(req: InjectRequest) -> dict:
    """注入确认（受控注入：confirm_token 校验 + 聚焦校验 + 剪贴板 + SendInput）"""
    if pipeline is None:
        raise HTTPException(503, {"code": 50301, "message": "brain pipeline 未初始化"})
    try:
        result = await pipeline.confirm_inject(
            task_id=req.task_id,
            decision=req.decision,
            confirm_token=req.confirm_token,
            manual=req.manual,
        )
    except BrainError as e:
        _raise(e)
    return {"code": 0, "data": result, "message": "注入结果"}


@router.get("/tasks")
async def list_brain_tasks(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    """任务列表（分页；status 可选过滤）"""
    if pipeline is None:
        raise HTTPException(503, {"code": 50301, "message": "brain pipeline 未初始化"})
    return {"code": 0, "data": pipeline.list_tasks(status=status, page=page, limit=limit), "message": ""}
