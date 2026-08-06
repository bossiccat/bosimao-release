"""API 路由：飞书机器人事件订阅回调（P2 骨架，O-014 语音对话预留）

- POST /api/v1/push/feishu/callback  事件订阅回调
  ① URL 验证（challenge 回显）  ② 消息事件接收 → EventBus 广播 EVT_FEISHU_MSG

只编排不写业务：解析 body → 调 FeishuCallbackService → 组装响应（错误码 4xxxx）。
回调端点需公网可达（用户后续提供内网穿透/服务器方案，见 config/push.yaml 注释）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..config import FeishuConfig
from ..core.events import EventBus
from ..push.feishu_events import FeishuCallbackError, FeishuCallbackService


def create_feishu_router(bus: EventBus, cfg: FeishuConfig) -> APIRouter:
    """工厂：在 lifespan 内创建（bus 构建后才能挂载，与 routes_ws 同模式）"""
    router = APIRouter(prefix="/api/v1/push/feishu", tags=["feishu"])
    service = FeishuCallbackService(bus, verification_token=cfg.verification_token)

    @router.post("/callback")
    async def feishu_callback(req: dict[str, Any]) -> dict[str, Any]:
        """飞书事件订阅回调：URL 验证 / 消息事件接收"""
        try:
            return await service.handle(req)
        except FeishuCallbackError as e:
            raise HTTPException(
                e.status_code, {"code": e.status_code * 100 + 1, "message": str(e)}
            )

    return router
