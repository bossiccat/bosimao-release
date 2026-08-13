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
from .utils.crash_reporter import (
    build_fastapi_exception_handler,
    install_crash_hooks,
)
from .utils.logger import setup_logging
from .voice.config import load_voice
from .voice.privacy import privacy_runtime

setup_logging(app_config.settings.log_level)
logger = logging.getLogger(__name__)

# 阶段 E-1：未捕获异常落盘（sys.excepthook / threading.excepthook + FastAPI 全局兜底）
install_crash_hooks(app_config.settings.app_version)


def _build_secured_session_router():
    """商业语音安全签发路由（ADR-014 fail-closed）

    production=True 且缺 TLS/owner/sidecar/nonce/限流/TRTC 任一 → 拒绝启动；
    非生产也绝不装配匿名签发（缺凭据时端点运行时返回 50300）。
    """
    from pathlib import Path

    from .voice.auth import CredentialValidator
    from .voice.config import (
        ProductionGateError,
        SidecarCredentialConfigError,
        SidecarCredentialHashSet,
        VoiceSecurityConfig,
        build_sidecar_credential_hashes,
    )
    from .voice.devices import DeviceService
    from .voice.nonce import NonceService
    from .voice.privacy import PrivacyRuntimeActions, PrivacyService
    from .voice.rate_limit import RateLimitConfig, RateLimiter
    from .voice.rtc_session import RtcSessionConfig, RtcSessionService
    from .voice.storage import VoiceStore

    settings = app_config.settings
    sidecar_credentials: SidecarCredentialHashSet | None = None
    sidecar_hash = ""
    try:
        sidecar_credentials = build_sidecar_credential_hashes(
            current_secret=settings.voice_sidecar_credential,
            next_secret=settings.voice_sidecar_credential_next,
            next_enabled_at=settings.voice_sidecar_next_enabled_at,
            next_expires_at=settings.voice_sidecar_next_expires_at,
            config_revision=settings.voice_sidecar_config_revision,
        )
        sidecar_hash = sidecar_credentials.current_hash
    except SidecarCredentialConfigError as exc:
        if settings.voice_production:
            raise ProductionGateError("生产安全能力缺失: sidecar credential configuration") from exc
    security = VoiceSecurityConfig(
        production=settings.voice_production,
        tls_enabled=settings.voice_tls_enabled,
        owner_credential_hash=(
            CredentialValidator.hash_credential(settings.voice_owner_credential)
            if settings.voice_owner_credential else ""
        ),
        sidecar_credential_hash=sidecar_hash,
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=settings.trtc_sdkappid,
        trtc_secret_key=settings.trtc_secretkey,
    )
    store = VoiceStore(Path(settings.voice_db_path))
    store.initialize()
    service = RtcSessionService(
        RtcSessionConfig(
            sdk_app_id=settings.trtc_sdkappid,
            secret_key=settings.trtc_secretkey,
            room_prefix=settings.trtc_room_prefix or "jax-",
        )
    )
    validator = CredentialValidator(
        store, security.owner_credential_hash, sidecar_credentials
    )
    # 真实隐私 RuntimeActions（ADR-021 D4）：desktop_capture 走 late-bound orchestrator holder，
    # lifespan 里 privacy_runtime.bind(orch) 完成绑定；cloud/mic/background 为 no-op。
    privacy = PrivacyService(store, PrivacyRuntimeActions())
    return routes_voice.create_secured_voice_router(
        store=store,
        service=service,
        validator=validator,
        nonces=NonceService(store),
        limiter=RateLimiter(store, RateLimitConfig()),
        security=security,
        devices=DeviceService(store),
        privacy=privacy,
    )


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
    # 隐私桌面捕获动作的 late-bound orchestrator 绑定（ADR-021 D4）
    privacy_runtime.bind(orch)

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
    # 生产模式（VOICE_PRODUCTION=true）不注册 legacy 半双工网关：匿名 /pair、/ws/voice、
    # 旧匿名 /status 不可达（ADR-014 fail-closed；安全端点由 secured router 提供）
    if not app_config.settings.voice_production:
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
    version=app_config.settings.app_version,
    lifespan=lifespan,
)

# 阶段 E-1：全局未捕获异常兜底（路由内未 try/except 的异常 → 落盘 + 统一 500）
app.add_exception_handler(
    Exception, build_fastapi_exception_handler(app_config.settings.app_version)
)

app.include_router(routes_status.router)
app.include_router(routes_control.router)
app.include_router(routes_capture.router)
app.include_router(routes_brain.router)

# 商业语音安全签发（ADR-012/014 + SPEC §5）：Bearer/nonce/限流/fail-closed，
# 不装配匿名 /session 与 /session/sign；production 缺必需能力时拒绝启动
app.include_router(_build_secured_session_router())


@app.get("/health")
async def health() -> dict:
    orch: Orchestrator | None = getattr(app.state, "orchestrator", None)
    model_server = "up" if orch else "unknown"
    return {"status": "ok", "model_server": model_server}
