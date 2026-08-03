"""FastAPI 应用入口 + 生命周期（启停编排器、会话清理）"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import routes_control, routes_status, routes_ws
from .config import config as app_config
from .core.events import EventBus
from .core.orchestrator import Orchestrator
from .engine.llama_omni_client import LlamaOmniClient
from .engine.vision_analyzer import VisionAnalyzer
from .push.manager import PushManager
from .services.reminder_service import ReminderService
from .utils.logger import setup_logging

setup_logging(app_config.settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：构建各服务并挂载 WS 路由；停止：清理资源"""
    bus = EventBus()
    client = LlamaOmniClient(app_config.settings)

    push = PushManager(app_config.push, app_config.settings)
    reminder = ReminderService(app_config.reminder, bus, push)
    analyzer = VisionAnalyzer(client, max_width=app_config.monitors.capture.max_width)
    orch = Orchestrator(app_config, bus, client, analyzer, push, reminder)

    # 注入路由依赖
    routes_control.push_manager = push
    routes_control.orchestrator = orch

    # WS 路由依赖 bus，在 lifespan 内创建并挂载
    ws_router, _hub = routes_ws.create_ws_router(bus)
    app.include_router(ws_router)

    app.state.orchestrator = orch
    app.state.bus = bus

    await orch.start()
    try:
        yield
    finally:
        await orch.stop()
        await client.close()


app = FastAPI(
    title="贾克斯模式 - AI 智能体监控中枢",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(routes_status.router)
app.include_router(routes_control.router)


@app.get("/health")
async def health() -> dict:
    orch: Orchestrator | None = getattr(app.state, "orchestrator", None)
    model_server = "up" if orch else "unknown"
    return {"status": "ok", "model_server": model_server}
