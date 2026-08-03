"""API 路由：状态查询"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.state import state

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status")
async def get_status() -> dict:
    """全局状态汇总"""
    return state.to_dict()


@router.get("/status/sessions/{app_id}")
async def get_session(app_id: str) -> dict:
    """单个会话详情"""
    snap = state.get(app_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"app_id 不存在: {app_id}")
    return snap.to_dict()
