"""jax-voice-api：云端语音控制面（B 方案 / docs/plans/2026-09-07-voice-cloud-session-migration.md）

职责边界（与 PC 端 jax-backend 严格分工）：
- 承载：配对码签发 / 设备注册 / 会话签发（POST /api/v1/voice/session、/session/sign）
  —— 即手机 App 的全部控制面（session_base_url 指向本服务）。
- 不承载：音频路径（手机 ↔ PC sidecar 走 TRTC 云端，本服务只发 room_id+userSig）、
  MiniCPM-o 桥、本地模型、WS /ws/voice（那是 PC 局域网直连态）。

复用 backend/app 的商业安全路由（ADR-014 fail-closed）：同一套 guarded 语义、
nonce/限流/错误码/契约测试已验证过的代码原样上云，不自造第二套。
hello（mTLS 客户端证书绑定）与 agent-threads 路由不装配——它们是 PC 内部控制面。
ACK 上报（ack_reporter）在云端无 PC 后端时按既有语义优雅降级（None）。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# 容器内 /srv 布局：main.py 与 app/ 包同级
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings  # noqa: E402
from app.voice.auth import CredentialValidator  # noqa: E402
from app.voice.config import (  # noqa: E402
    ProductionGateError,
    SidecarCredentialConfigError,
    VoiceSecurityConfig,
    build_sidecar_credential_hashes,
    validate_voice_storage,
)
from app.voice.devices import DeviceService  # noqa: E402
from app.voice.nonce import NonceService  # noqa: E402
from app.voice.rate_limit import RateLimitConfig, RateLimiter  # noqa: E402
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService  # noqa: E402
from app.voice.store_factory import (  # noqa: E402
    build_voice_store,
    shutdown_voice_store,
)
from app.api.routes_voice_secured import create_secured_voice_router  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jax-voice-api")

settings = load_settings()

sidecar_credentials = None
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
    # 生产 fail-closed 与 PC 端 main.py 同语义：生产模式缺 sidecar 配置拒绝启动
    if settings.voice_production:
        raise ProductionGateError("生产安全能力缺失: sidecar credential configuration") from exc

security = VoiceSecurityConfig(
    production=settings.voice_production,
    # CloudRun 平台在边缘终结 TLS（默认域名即 HTTPS）；容器内明文，与平台边界约定一致
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
    # 生产门禁要求项：云端无长连会话，termination 走 ledger 语义（无 ACK 时 revoke 可重试 50301）
    rtc_termination_enabled=os.environ.get(
        "RTC_TERMINATION_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes"),
)

# 存储边界门禁（ADR fail-closed）：必须在任何 store 构造之前执行。
# SQLite 只允许作为显式开发/测试夹具；生产必须声明 PostgreSQL 并提供私密 DSN。
validate_voice_storage(
    production=settings.voice_production,
    storage_backend=settings.voice_storage_backend,
    database_url=settings.voice_database_url,
)
# production PostgreSQL adapter：已由 app.voice.store_factory 装配进本入口——
# DSN 必须是 postgresql://，psycopg 缺失抛 PsycopgNotAvailableError，
# 绝不 silently 回退本地存储；非生产才走下面的 SQLite 夹具。


def _build_store():
    """development-only store fixture（仅非生产路径使用）。"""
    from app.voice.storage import VoiceStore

    fixture = VoiceStore(Path(settings.voice_db_path))
    fixture.initialize()
    return fixture


store = build_voice_store(settings, sqlite_factory=_build_store)

service = RtcSessionService(
    RtcSessionConfig(
        sdk_app_id=settings.trtc_sdkappid,
        secret_key=settings.trtc_secretkey,
        room_prefix=settings.trtc_room_prefix or "jax-",
    )
)
validator = CredentialValidator(store, security.owner_credential_hash, sidecar_credentials)

secured_router = create_secured_voice_router(
    store=store,
    service=service,
    validator=validator,
    nonces=NonceService(store),
    limiter=RateLimiter(store, RateLimitConfig()),
    security=security,
    devices=DeviceService(store),
    # hello_service=None：hello（PC 桥 mTLS 绑定）不上云；privacy 用容器内默认（no-op actions）
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """优雅关闭：生产路径的 PG 连接池必须释放（SQLite 夹具无 close，no-op）。"""
    try:
        yield
    finally:
        await shutdown_voice_store(store)


app = FastAPI(
    title="jax-voice-api",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(secured_router)


@app.get("/health")
async def health() -> dict:
    # 不输出任何凭据/密钥；configured 只反映能力是否齐备
    return {
        "status": "ok",
        "service": "jax-voice-api",
        "trtc_configured": service.is_configured,
        "security_ready": not security_missing(),
    }


def security_missing() -> list[str]:
    from app.voice.config import runtime_missing
    return runtime_missing(security)


@app.get("/api/v1/voice/cloud/status")
async def cloud_status() -> dict:
    """部署核验用：只报能力位与版本，无敏感信息"""
    return {
        "code": 0,
        "data": {
            "service": "jax-voice-api",
            "production": settings.voice_production,
            "trtc_configured": service.is_configured,
            "security_missing": security_missing(),
        },
        "message": "",
    }


@app.exception_handler(ProductionGateError)
async def gate_error_handler(_req, exc: ProductionGateError):
    return JSONResponse(status_code=503, content={"code": 50300, "data": None, "message": str(exc)})
