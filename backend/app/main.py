"""FastAPI 应用入口 + 生命周期（启停编排器、会话清理）"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import (
    routes_brain,
    routes_capture,
    routes_control,
    routes_feishu,
    routes_status,
    routes_voice,
    routes_ws,
)
from .brain.deepseek_client import DeepSeekClient
from .brain.injector import Injector
from .brain.intent_service import IntentService
from .brain.pipeline import BrainPipeline
from .brain.store import TaskStore
from .brain.task_service import TaskService
from .config import config as app_config
from .core.events import EventBus
from .core.orchestrator import Orchestrator
from .engine.llama_omni_client import LlamaOmniClient
from .engine.vision_analyzer import VisionAnalyzer
from .push.manager import PushManager
from .services.reminder_service import ReminderService
from .utils.logger import setup_logging
from .voice.config import load_voice

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
    routes_capture.orchestrator = orch

    # V1.5 大脑闭环（O-011/O-012/O-013）
    deepseek = DeepSeekClient(app_config.settings, app_config.brain)
    brain_store = TaskStore()
    intent_svc = IntentService(client, app_config.brain)
    task_svc = TaskService(deepseek, app_config.brain)
    injector = Injector(app_config.brain, app_config.monitors)
    brain_pipeline = BrainPipeline(
        app_config.brain, deepseek, intent_svc, task_svc, brain_store, injector, bus
    )
    routes_brain.pipeline = brain_pipeline

    # WS 路由依赖 bus，在 lifespan 内创建并挂载
    ws_router, _hub = routes_ws.create_ws_router(bus)
    app.include_router(ws_router)

    # voice 网关（mobile-voice-spec §8）：WS /ws/voice + 控制面（半双工 M2 / 全双工 M3 占位）
    voice_cfg = load_voice(app_config.settings.voice_token, app_config.settings.voice_e2ee_key)
    voice_router, _voice_mgr = routes_voice.build_voice_gateway(voice_cfg)
    app.include_router(voice_router)

    # 飞书事件订阅回调（O-014 语音对话预留，P2 骨架）
    feishu_router = routes_feishu.create_feishu_router(bus, app_config.push.feishu)
    app.include_router(feishu_router)

    app.state.orchestrator = orch
    app.state.bus = bus
    app.state.brain_pipeline = brain_pipeline

    await orch.start()
    try:
        yield
    finally:
        await orch.stop()
        await client.close()
        await deepseek.close()


app = FastAPI(
    title="贾克斯模式 - AI 智能体监控中枢",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(routes_status.router)
app.include_router(routes_control.router)
app.include_router(routes_capture.router)
app.include_router(routes_brain.router)

# TRTC 会话签发（ADR-012 / PC-INTEGRATION §2.3）：仅依赖 .env，可独立于 lifespan 装配
app.include_router(routes_voice.build_session_router())


@app.get("/health")
async def health() -> dict:
    orch: Orchestrator | None = getattr(app.state, "orchestrator", None)
    model_server = "up" if orch else "unknown"
    return {"status": "ok", "model_server": model_server}
