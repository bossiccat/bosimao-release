"""FastAPI 应用入口 + 生命周期（启停编排器、会话清理）"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from .brain.agent_thread_registry import AgentThreadRegistry
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
from .voice.auth import CredentialValidator
from .voice.privacy import privacy_runtime
from .voice.trusted_gateway import TrustedGatewayIdentityMiddleware

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
        validate_voice_storage,
    )
    from .voice.devices import DeviceService
    from .voice.hello_runtime import build_hello_runtime
    from .voice.nonce import NonceService
    from .voice.privacy import PrivacyRuntimeActions, PrivacyService
    from .voice.rate_limit import RateLimitConfig, RateLimiter
    from .voice.rtc_session import RtcSessionConfig, RtcSessionService
    from .voice.storage import VoiceStore
    from .brain.agent_thread_registry import AgentThreadRegistry
    from .api.routes_agent_threads import create_agent_thread_router

    settings = app_config.settings
    validate_voice_storage(
        production=settings.voice_production,
        storage_backend=settings.voice_storage_backend,
        database_url=settings.voice_database_url,
    )
    if settings.voice_production:
        # The PostgreSQL adapter is intentionally fail-closed until the
        # connection pool and repository contract are wired into this entrypoint.
        raise ProductionGateError(
            "production PostgreSQL adapter is not wired into the voice entrypoint"
        )
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
        store, security.owner_credential_hash, sidecar_credentials,
        rtc_bridge_credential_hash=(
            CredentialValidator.hash_credential(settings.voice_rtc_bridge_credential)
            if settings.voice_rtc_bridge_credential else ""
        ),
        brain_service_credential_hash=(
            CredentialValidator.hash_credential(settings.voice_brain_service_credential)
            if settings.voice_brain_service_credential else ""
        ),
    )
    hello_runtime = build_hello_runtime(
        store=store,
        production=settings.voice_production,
        private_key_pem=settings.voice_hello_private_key_pem,
        public_key_pem=settings.voice_hello_public_key_pem,
        rtc_bridge_credential=settings.voice_rtc_bridge_credential,
        certificate_binding=settings.voice_rtc_bridge_cert_binding,
        gateway_assertion=settings.voice_gateway_shared_assertion,
    )
    # 真实隐私 RuntimeActions（ADR-021 D4）：desktop_capture 走 late-bound orchestrator holder，
    # lifespan 里 privacy_runtime.bind(orch) 完成绑定；cloud/mic/background 为 no-op。
    privacy = PrivacyService(store, PrivacyRuntimeActions())
    secured_router = routes_voice.create_secured_voice_router(
        store=store,
        service=service,
        validator=validator,
        nonces=NonceService(store),
        limiter=RateLimiter(store, RateLimitConfig()),
        security=security,
        devices=DeviceService(store),
        privacy=privacy,
        hello_service=hello_runtime.service,
        hello_certificate_binding=hello_runtime.certificate_binding,
        hello_gateway_assertion_hash=hello_runtime.gateway_assertion_hash,
    )
    secured_router.include_router(create_agent_thread_router(
        registry=AgentThreadRegistry(), validator=validator,
        nonces=NonceService(store), limiter=RateLimiter(store, RateLimitConfig()),
    ))
    return secured_router


_LOOP_LAG_WARN_S = 2.0
_LOOP_LAG_POLL_S = 5.0


async def _watch_loop_lag(loop: asyncio.AbstractEventLoop) -> None:
    """事件循环 lag 探针：sleep(poll) 的实际唤醒间隔减去 poll 即 lag。

    真机实证（2026-09-05 19:18-19:20）：loop 被同步慢调用阻塞 111s，
    hello proof（TTL 60s）过期 → 兑付 40112 → sidecar 退出。此类阻塞
    必须留 WARNING 证据，否则无从归因。
    """
    last = loop.time()
    while True:
        await asyncio.sleep(_LOOP_LAG_POLL_S)
        now = loop.time()
        lag = now - last - _LOOP_LAG_POLL_S
        last = now
        if lag >= _LOOP_LAG_WARN_S:
            logger.warning(
                "event loop lag %.2fs detected (threshold %.1fs) — 同步慢调用占用事件循环",
                lag, _LOOP_LAG_WARN_S,
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
    app.state.agent_thread_registry = AgentThreadRegistry()

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

    # 事件循环 lag 监控（2026-09-05）：真机实证 backend loop 被阻塞 111s（19:18:39
    # → 19:20:25），hello proof TTL=60s 在等待中过期 → 兑付 40112。同步状态机已
    # 挪线程池，但任何新同步慢调用都会复发——lag 超阈值必须留证据。
    await orch.start()
    loop = asyncio.get_running_loop()
    loop_lag_task = loop.create_task(_watch_loop_lag(loop))
    try:
        yield
    finally:
        loop_lag_task.cancel()
        await orch.stop()
        await client.close()
        await deepseek.close()


app = FastAPI(
    title="贾克斯模式 - AI 智能体监控中枢",
    version=app_config.settings.app_version,
    lifespan=lifespan,
)

# sidecar renderer CORS（RP-07 后续修复，2026-09-02）：
# rtc.js/phone.js 在 file:// 页面（Origin: null）用 Chromium fetch 调控制面，
# 自定义头触发 OPTIONS preflight，无 CORS 中间件时 405 → "Failed to fetch"。
# fail-closed：仅放行 file:// 的 Origin 字面量 "null"；普通网页 Origin 不放行；
# allow_credentials=False（Bearer 控制面，无 cookie）。preflight 由中间件应答、
# 不进业务守卫；实请求鉴权语义不变（无凭证仍 401）。
# 契约：backend/tests/contract/test_sidecar_renderer_cors_contract.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "X-Request-Nonce", "Content-Type"],
    allow_credentials=False,
)

# 阶段 E-1：全局未捕获异常兜底（路由内未 try/except 的异常 → 落盘 + 统一 500）
app.add_exception_handler(
    Exception, build_fastapi_exception_handler(app_config.settings.app_version)
)
app.add_middleware(
    TrustedGatewayIdentityMiddleware,
    gateway_assertion_hash=(
        CredentialValidator.hash_credential(
            app_config.settings.voice_gateway_shared_assertion
        ) if app_config.settings.voice_gateway_shared_assertion else ""
    ),
    certificate_binding=app_config.settings.voice_rtc_bridge_cert_binding,
    allowed_hosts=[
        host.strip()
        for host in app_config.settings.voice_trusted_gateway_hosts.split(",")
        if host.strip()
    ],
)

app.include_router(routes_status.router)
app.include_router(routes_control.router)
app.include_router(routes_capture.router)
app.include_router(routes_brain.router)

# 商业语音安全签发（ADR-012/014 + SPEC §5）：Bearer/nonce/限流/fail-closed，
# 不装配匿名 /session 与 /session/sign；production 缺必需能力时拒绝启动
app.include_router(_build_secured_session_router())


# A10（2026-08-21 numpy 事故）：/health 带进程身份签名。
# 事故机制：临时 python 进程占用 :8000 且 /health 返回 200 → 启动脚本幂等放行，
# 模型服务实际不可用但被判定健康。修复：响应携带 proc_name/pid/run_id，
# 消费方可核对"应答进程是否就是期望进程"，防止端口被外来进程劫持后冒名。
# 模块级只算一次（进程生命周期内不变；uvicorn reload 模式重启 worker 会换新 run_id，属预期）。
_PROC_NAME = os.path.basename(sys.executable) or "unknown"
_PROC_PID = os.getpid()
_RUN_ID = uuid.uuid4().hex[:8]


@app.get("/health")
async def health() -> dict:
    orch: Orchestrator | None = getattr(app.state, "orchestrator", None)
    model_server = "up" if orch else "unknown"
    return {
        "status": "ok",
        "model_server": model_server,
        "proc_name": _PROC_NAME,
        "pid": _PROC_PID,
        "run_id": _RUN_ID,
    }
