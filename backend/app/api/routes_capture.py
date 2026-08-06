"""API 路由：WGC 授权流程（触发授权 / 授权状态查询）

契约：docs/specs/backend-capture-auth-spec.md §3
- POST /api/v1/capture/authorize {app_id, retry?} → 触发授权（幂等）
- GET  /api/v1/capture/status → 各窗口授权状态
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/capture", tags=["capture"])

# 由 main 注入（避免循环依赖）
orchestrator = None  # type: ignore[assignment]


class AuthorizeRequest(BaseModel):
    app_id: str
    retry: bool = False  # true=手动重试已拒绝窗口（重置 denied/status-only）


@router.post("/authorize")
async def authorize_capture(req: AuthorizeRequest) -> dict:
    """触发指定窗口 WGC 授权（进入 authorizing → WS auth_prompt → 系统选择器）。"""
    if orchestrator is None:
        raise HTTPException(503, "orchestrator 未初始化")
    res = await orchestrator.authorize_capture(req.app_id, req.retry)
    code = res.get("code")
    if code in (40401, 40402):
        raise HTTPException(404, res["error"])
    if code in (40901, 40902):
        raise HTTPException(409, res["error"])
    if code == 40001:
        raise HTTPException(400, res["error"])
    return {"accepted": True, "app_id": req.app_id, "mode": res.get("mode", "authorizing")}


@router.get("/status")
async def capture_status() -> dict:
    """各窗口授权状态（对照 session_manager.mode：pending-auth/authorizing/authorized/denied/status-only/wgc/dxgi/lost/none）"""
    if orchestrator is None:
        raise HTTPException(503, "orchestrator 未初始化")
    return {"sessions": orchestrator.capture_status()}
