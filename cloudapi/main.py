"""jax-voice-api：云端语音控制面（B 方案 / docs/plans/2026-09-07-voice-cloud-session-migration.md）

职责边界（与 PC 端 jax-backend 严格分工）：
- 承载：配对码签发 / 设备注册 / 会话签发（POST /api/v1/voice/session、/session/sign）
  —— 即手机 App 的全部控制面（session_base_url 指向本服务）。
- 不承载：音频路径（手机 ↔ PC sidecar 走 TRTC 云端，本服务只发 room_id+userSig）、
  MiniCPM-o 桥、本地模型、WS /ws/voice（那是 PC 局域网直连态）。

复用 backend/app 的商业安全路由（ADR-014 fail-closed）：同一套 guarded 语义、
nonce/限流/错误码/契约测试已验证过的代码原样上云，不自造第二套。
hello proof 的**签发与兑付**都必须在本服务器装配：session / pending claim / hello proof
是本服务职责（docs/plans/2026-09-07-voice-cloud-session-migration.md §2），部署 Secret
注入包含 hello signing。不装配 hello 会让 SecuredVoiceDeps.sidecar_sign 恒为 None，
POST /api/v1/voice/session/sign 恒返 50303 —— 整条云端语音链路是死的。
兑付端 POST /api/v1/voice/internal/rtc-bridge/hello-redeem 需要两项服务端装配（与 PC
入口 backend/app/main.py 同一套机制，不自造第二套、不放松校验）：
  1. 受信网关身份中间件 TrustedGatewayIdentityMiddleware —— 解析
     VOICE_TRUSTED_GATEWAY_HOSTS 并把受信来源+正确断言盖章进 scope，
     供 trusted_certificate_binding(scope) 读取；否则兑付端拿不到盖章值恒 40114。
  2. CredentialValidator 传 rtc_bridge_credential_hash —— 缺失时
     verify_rtc_bridge(bearer) 恒 40101（backend/app/voice/auth.py:161-166）。
agent-threads 路由不装配——它是 PC 内部控制面。
ACK 上报（ack_reporter）在云端无 PC 后端时按既有语义优雅降级（None）。
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
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
from app.voice.hello_runtime import build_hello_runtime  # noqa: E402
from app.voice.nonce import NonceService  # noqa: E402
from app.voice.rate_limit import RateLimitConfig, RateLimiter  # noqa: E402
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService  # noqa: E402
from app.voice.store_factory import (  # noqa: E402
    build_voice_store,
    shutdown_voice_store,
)
from app.voice.trusted_gateway import TrustedGatewayIdentityMiddleware  # noqa: E402
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
validator = CredentialValidator(
    store, security.owner_credential_hash, sidecar_credentials,
    # 兑付端 hello-redeem 的 rtc_bridge 服务身份（与 PC 入口 backend/app/main.py:135-138
    # 同一算法/来源）：缺此哈希时 verify_rtc_bridge 恒 40101，兑付 fail-closed 终局。
    rtc_bridge_credential_hash=(
        CredentialValidator.hash_credential(settings.voice_rtc_bridge_credential)
        if settings.voice_rtc_bridge_credential else ""
    ),
)

# hello proof 签发运行时（复用 PC 入口同一套 build_hello_runtime，不自造第二套）。
# - 生产模式：任一 hello 配置键缺失 → 启动即拒（ProductionGateError），绝不在请求期
#   退化成 50303（那正是本次缺陷）。异常只列缺失的**配置键名**，不含密钥值。
# - 非生产：缺键时 hello 不装配（hello_service=None），保持本地开发/测试可跑。
_HELLO_REQUIRED_SETTINGS = (
    ("VOICE_HELLO_PRIVATE_KEY_PEM", settings.voice_hello_private_key_pem),
    ("VOICE_HELLO_PUBLIC_KEY_PEM", settings.voice_hello_public_key_pem),
    ("VOICE_RTC_BRIDGE_CREDENTIAL", settings.voice_rtc_bridge_credential),
    ("VOICE_RTC_BRIDGE_CERT_BINDING", settings.voice_rtc_bridge_cert_binding),
    ("VOICE_GATEWAY_SHARED_ASSERTION", settings.voice_gateway_shared_assertion),
)
_missing_hello_settings = [name for name, value in _HELLO_REQUIRED_SETTINGS if not value]
hello_runtime = None
if _missing_hello_settings:
    if settings.voice_production:
        raise ProductionGateError(
            "生产安全能力缺失: hello signing 配置缺失: "
            + ", ".join(_missing_hello_settings)
        )
else:
    hello_runtime = build_hello_runtime(
        store=store,
        production=settings.voice_production,
        private_key_pem=settings.voice_hello_private_key_pem,
        public_key_pem=settings.voice_hello_public_key_pem,
        rtc_bridge_credential=settings.voice_rtc_bridge_credential,
        certificate_binding=settings.voice_rtc_bridge_cert_binding,
        gateway_assertion=settings.voice_gateway_shared_assertion,
    )

secured_router = create_secured_voice_router(
    store=store,
    service=service,
    validator=validator,
    nonces=NonceService(store),
    limiter=RateLimiter(store, RateLimitConfig()),
    security=security,
    devices=DeviceService(store),
    # hello 装配是 /session/sign 可用性的前提：不传会让 deps.sidecar_sign 恒为 None → 50303。
    hello_service=hello_runtime.service if hello_runtime is not None else None,
    hello_certificate_binding=(
        hello_runtime.certificate_binding if hello_runtime is not None else ""
    ),
    hello_gateway_assertion_hash=(
        hello_runtime.gateway_assertion_hash if hello_runtime is not None else ""
    ),
    # 外带上行 ingest ticket 签发器：与 hello 同一把私钥（aud 区分），不新增密钥材料。
    # 装配在此即生效的前提是 hello 运行时可用；未装配时端点返回 503（惰性，不外泄语义）。
    ingest_ticket_signer=(
        hello_runtime.ingest_signer if hello_runtime is not None else None
    ),
    # privacy 用容器内默认（no-op actions）
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

# 受信网关身份中间件（与 PC 入口 backend/app/main.py:293-306 同一套机制与解析方式）：
# 兑付端 trusted_certificate_binding(request.scope) 只认本中间件在「来源受信 + 断言
# HMAC 匹配」双条件下盖章的 scope 值。云端边缘 LB 是 IP 池，来源经
# VOICE_TRUSTED_GATEWAY_HOSTS 显式声明（支持单 IP 与 CIDR）；缺此中间件时
# hello-redeem 恒 40114（fail-closed）。断言哈希只在这里算，密钥值不进日志。
app.add_middleware(
    TrustedGatewayIdentityMiddleware,
    gateway_assertion_hash=(
        CredentialValidator.hash_credential(settings.voice_gateway_shared_assertion)
        if settings.voice_gateway_shared_assertion else ""
    ),
    certificate_binding=settings.voice_rtc_bridge_cert_binding,
    allowed_hosts=[
        host.strip()
        for host in settings.voice_trusted_gateway_hosts.split(",")
        if host.strip()
    ],
)

app.include_router(secured_router)


@app.get("/health")
async def health() -> dict:
    # 不输出任何凭据/密钥；configured 只反映能力是否齐备
    return {
        "status": "ok",
        "service": "jax-voice-api",
        "trtc_configured": service.is_configured(),
        "security_ready": not security_missing(),
    }


def security_missing() -> list[str]:
    from app.voice.config import runtime_missing
    return runtime_missing(security)


def storage_probe() -> str:
    """存储可用性探针：按步骤逐一探测控制面写入路径，返回 `label=状态` 列表。

    存在的理由：`/health` 只证明进程活着，不代表存储可用。线上曾出现服务部署成功、
    `/health` 200，但任何用到存储的端点都 500；而且读能过、写会挂——只探一种路径
    会把问题看漏。逐步骤探测能把「哪一步、哪种异常」直接钉出来，不必靠猜。
    只返回**异常类型名**：足够区分池状态 / 网络 / 类型 / 权限 / 方言问题，
    又不把内部细节（表名、SQL 片段）长期暴露在响应里。
    """
    import time as _time
    import uuid as _uuid

    steps: list[str] = []

    def run(label: str, fn) -> None:
        try:
            fn()
            steps.append(f"{label}=ok")
        except Exception as exc:
            steps.append(f"{label}={type(exc).__name__}")

    run("read", lambda: store.get_setting("__storage_probe__"))
    run("write", lambda: store.set_setting("__storage_probe__", "1"))
    run("nonce", lambda: store.consume_nonce("__probe__", _uuid.uuid4().hex, ttl_seconds=60))
    run("limit", lambda: store.rate_limit.increment("__probe__", "__probe__", _time.time()))
    run("pairing", lambda: store.create_pairing_code("__probe__", "android", 60))
    return " | ".join(steps)


@app.get("/api/v1/voice/cloud/status")
async def cloud_status() -> dict:
    """部署核验用：只报能力位与版本，无敏感信息"""
    return {
        "code": 0,
        "data": {
            "service": "jax-voice-api",
            "production": settings.voice_production,
            "trtc_configured": service.is_configured(),
            "security_missing": security_missing(),
            "storage": storage_probe(),
        },
        "message": "",
    }


@app.exception_handler(ProductionGateError)
async def gate_error_handler(_req, exc: ProductionGateError):
    return JSONResponse(status_code=503, content={"code": 50300, "data": None, "message": str(exc)})


# 自检写入统一使用该命名空间与短 TTL，绝不触碰真实业务数据。
_SELFCHECK_NAMESPACE = "__selfcheck__"


@app.post("/api/v1/voice/cloud/selfcheck")
async def cloud_selfcheck(request: Request) -> dict:
    """应用自检：由**服务自己**在云端把控制面写入链路真跑一遍，逐步报告结果。

    为什么放在产品里而不是外部脚本：验证「云端是否真能用」必须由服务自身对真实
    数据库执行，而不是靠本地脚本打接口再自行解读——后者等于用脚本解题，也测不到
    服务内的装配、事务与方言行为。发布门禁与运维巡检可直接消费本端点的 `ok`。

    步骤：nonce 消费 → 限流自增 → 配对码签发 → 配对码消费 → 审计写入。
    全部使用 `__selfcheck__` 命名空间 + 短 TTL；失败只回异常类型名，不回消息。
    """
    import time as _time
    import uuid as _uuid

    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return JSONResponse(
            status_code=401,
            content={"code": 40101, "data": None, "message": "owner credential required"},
        )
    try:
        validator.verify_owner(token.strip())
    except Exception as exc:
        return JSONResponse(
            status_code=401,
            content={
                "code": getattr(exc, "code", 40101),
                "data": None,
                "message": "invalid owner credential",
            },
        )

    steps: dict[str, str] = {}
    issued: dict[str, str] = {}

    def step(name: str, fn) -> None:
        try:
            fn()
            steps[name] = "ok"
        except Exception as exc:
            steps[name] = type(exc).__name__

    def create_pairing_code() -> None:
        code, _meta = store.create_pairing_code(_SELFCHECK_NAMESPACE, "selfcheck", 60)
        issued["code"] = code

    step("nonce", lambda: store.consume_nonce(
        _SELFCHECK_NAMESPACE, _uuid.uuid4().hex, ttl_seconds=60))
    step("rate_limit", lambda: store.rate_limit.increment(
        _SELFCHECK_NAMESPACE, _SELFCHECK_NAMESPACE, _time.time()))
    step("pairing_create", create_pairing_code)
    if steps["pairing_create"] == "ok":
        step("pairing_consume", lambda: store.consume_pairing_code(
            issued["code"], _SELFCHECK_NAMESPACE))
    else:
        steps["pairing_consume"] = "skipped"
    step("audit", lambda: store.write_audit(
        "selfcheck", "system", _SELFCHECK_NAMESPACE, "ok", {}))

    return {
        "code": 0,
        "data": {"ok": all(v == "ok" for v in steps.values()), "steps": steps},
        "message": "",
    }
