"""API 路由：控制指令（启停监控 / 测试推送 / 配置热重载）"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import config as app_config
from ..push.manager import PushManager

router = APIRouter(prefix="/api/v1", tags=["control"])

# 由 main 注入（避免循环依赖）
push_manager: PushManager | None = None
orchestrator = None  # type: ignore[assignment]


class ControlCommand(BaseModel):
    action: str  # start_monitoring / stop_monitoring / pause / resume / trigger_alert_test
    target: str | None = None


@router.post("/control")
async def send_control(cmd: ControlCommand) -> dict:
    if cmd.action == "trigger_alert_test":
        # 手动触发 4 级提醒测试（验证桌宠 + 推送链路）→ 走 Orchestrator 公开 API
        if orchestrator is None:
            raise HTTPException(503, "orchestrator 未初始化")
        from ..core.state import state

        target = cmd.target or (state.all()[0].app_id if state.all() else "codex")
        ok = await orchestrator.trigger_test_alert(target, level=4)
        if not ok:
            raise HTTPException(404, f"未知 app_id: {target}")
        return {"accepted": True, "action": "trigger_alert_test", "target": target}
    if cmd.action in ("start_monitoring", "resume_monitoring"):
        if orchestrator is None:
            raise HTTPException(503, "orchestrator 未初始化")
        orchestrator.start_monitoring()
        return {"accepted": True}
    if cmd.action in ("stop_monitoring", "pause_monitoring"):
        if orchestrator is None:
            raise HTTPException(503, "orchestrator 未初始化")
        orchestrator.stop_monitoring()
        return {"accepted": True}
    raise HTTPException(400, f"未知 action: {cmd.action}")


@router.post("/control/test-push")
async def test_push() -> dict:
    """手动测试手机推送可达性"""
    if push_manager is None:
        raise HTTPException(503, "推送未初始化")
    result = push_manager.push(text="贾克斯模式推送链路测试 OK", title="测试推送")
    return {"ok": result.ok, "provider": result.provider, "error": result.error}


@router.post("/config/reload")
async def reload_config() -> dict:
    """热重载 config/*.yaml"""
    errors = app_config.reload()
    return {"ok": not errors, "errors": errors}
