"""API 路由：状态查询"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import config as app_config
from ..core.state import state

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status")
async def get_status() -> dict:
    """全局状态汇总"""
    data = state.to_dict()
    # 追加 detection 配置快照（e2e 验证"改配置即生效"；不含敏感项）
    data["config"] = {
        "detection": {
            "stuck_frame_threshold": app_config.detection.stuck_frame_threshold,
            "stuck_timeout_seconds": app_config.detection.stuck_timeout_seconds,
            "off_track_frame_threshold": app_config.detection.off_track_frame_threshold,
            "min_alert_interval_seconds": app_config.detection.min_alert_interval_seconds,
            "max_alerts_per_hour": app_config.detection.max_alerts_per_hour,
        }
    }
    return data


@router.get("/status/sessions/{app_id}")
async def get_session(app_id: str) -> dict:
    """单个会话详情"""
    snap = state.get(app_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"app_id 不存在: {app_id}")
    return snap.to_dict()
