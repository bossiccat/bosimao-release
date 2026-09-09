"""配置系统：统一加载 config/*.yaml + .env（pydantic-settings）"""
from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ._frozen_paths import project_root

PROJECT_ROOT = project_root()
CONFIG_DIR = PROJECT_ROOT / "config"


class MonitorTarget(BaseModel):
    """单个监控目标（对应 config/monitors.yaml 一项）"""

    app_id: str
    app_name: str
    process_name: str
    window_title_regex: str = ".*"
    poll_interval_seconds: int = 6
    enabled: bool = True


class CaptureConfig(BaseModel):
    max_width: int = 1280
    jpeg_quality: int = 85
    tmp_dir: str = "./tmp/captures"


class DetectionConfig(BaseModel):
    """检测参数（对应 config/detection.yaml）"""

    stuck_frame_threshold: int = 3
    stuck_timeout_seconds: int = 120
    off_track_frame_threshold: int = 2
    prompt_template: str = "./config/prompts/vision_analyze.md"
    min_alert_interval_seconds: int = 60
    max_alerts_per_hour: int = 30


class ReminderConfig(BaseModel):
    level_1_status_dot: bool = True
    level_2_pet_move: bool = True
    level_3_pet_alert: bool = True
    level_4_voice_push: bool = True
    voice_alert_enabled: bool = True
    push_alert_enabled: bool = True


class WecomConfig(BaseModel):
    enabled: bool = True
    rate_limit_per_minute: int = 15


class FeishuConfig(BaseModel):
    """飞书机器人配置（O-002 选型：企微→飞书；对应 config/push.yaml feishu 段）

    webhook_url 优先读 .env FEISHU_WEBHOOK_URL；此处为 yaml 兜底。
    verification_token 用于事件订阅回调验签（用户创建飞书自建应用后填写，可选）。
    """

    enabled: bool = True
    rate_limit_per_minute: int = 100   # 飞书官方 ~100/min，留余量
    webhook_url: str = ""              # 备选：直接填完整 webhook（优先 .env）
    verification_token: str = ""       # 事件订阅验证 token（可选）


class NtfyConfig(BaseModel):
    enabled: bool = True
    server: str = "https://ntfy.sh"
    priority: str = "default"
    with_screenshot: bool = True


class CircuitBreakerConfig(BaseModel):
    fail_threshold: int = 3
    cooldown_seconds: int = 300


class PushConfig(BaseModel):
    providers: list[str] = ["feishu", "ntfy"]
    wecom: WecomConfig = Field(default_factory=WecomConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


class MonitorsConfig(BaseModel):
    monitors: list[MonitorTarget] = Field(default_factory=list)
    voice_active_poll_interval_seconds: int = 12
    capture: CaptureConfig = Field(default_factory=CaptureConfig)


class DeepSeekConfig(BaseModel):
    """DeepSeek 客户端参数（config/brain.yaml → deepseek 组，非敏感）"""

    enabled: bool = True
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    total_timeout_s: float = 60.0
    network_retries: int = 2          # 网络类错误重试 2 次（退避 1s、2s）
    network_backoff_s: list[float] = [1.0, 2.0]
    http_retries: int = 1             # 429/5xx 重试 1 次（退避 3s）
    http_backoff_s: float = 3.0
    circuit_fail_threshold: int = 3   # 连续失败 ≥3 → 熔断
    circuit_cooldown_s: int = 300     # 熔断 300s
    max_tokens_decompose: int = 2048
    max_tokens_instruct: int = 2048
    max_tokens_review: int = 1024
    max_tokens_health: int = 8


class IntentConfig(BaseModel):
    """意图理解（本地 9B）参数"""

    extract_prompt: str = "./config/prompts/intent_extract.md"
    summary_max_chars: int = 1200     # 摘要 ≤1200 字（上传 DeepSeek 硬上限）
    local_max_tokens: int = 256
    clarify_threshold: float = 0.6
    max_clarify_rounds: int = 2


class ReviewConfig(BaseModel):
    enabled: bool = True
    interval_frames: int = 5
    milestone_summary_max_chars: int = 600


class InjectConfig(BaseModel):
    """受控注入（O-012/O-013）参数"""

    target_app: str = "codex"
    clipboard_delay_s: float = 0.15   # SetClipboardData → SendInput 竞态规避（勿删）
    enter_delay_s: float = 0.1
    expire_seconds: int = 300         # awaiting_confirm 起 300s 未确认 → expired
    regenerate_limit_seconds: int = 60
    instructions_dir: str = "./backend/data/instructions"
    audit_path: str = "./backend/data/inject_audit.jsonl"
    audit_preview_chars: int = 60


class ReportConfig(BaseModel):
    throttle_seconds: int = 60


class BrainConfig(BaseModel):
    """大脑配置聚合（config/brain.yaml → brain 根）"""

    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    intent: IntentConfig = Field(default_factory=IntentConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    inject: InjectConfig = Field(default_factory=InjectConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)


class Settings(BaseSettings):
    """环境变量（.env）+ 全局配置"""

    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    model_server_host: str = "127.0.0.1"
    model_server_port: int = 19080
    model_dir: str = "D:\\models\\MiniCPM-o-4_5-gguf"
    model_file: str = "MiniCPM-o-4_5-Q4_K_M.gguf"
    # PoC B1 结论：ctx 4096（8192 显存 12019MB 超 12G 限）
    model_ctx_size: int = 4096
    model_ngl: int = 99

    # 模型服务超时/重试（backend-llama-client-spec §4）
    model_connect_timeout_s: float = 10.0      # 连接/首字节
    model_prefill_timeout_s: float = 60.0      # init/prefill（首轮冷启动 35s）
    model_stream_idle_timeout_s: float = 20.0  # decode 流相邻块间隔
    model_round_timeout_s: float = 120.0       # 整轮上限（prefill+decode）
    model_retry_count: int = 1                 # 网络类错误重试次数
    model_retry_backoff_s: float = 1.0         # 重试退避秒

    backend_port: int = 8000

    app_version: str = "1.0.0"

    # Production voice requires both TLS paths and HTTPS/WSS uvicorn configuration.
    tls_certfile: str = ""     # 服务端证书（如 certs/server.crt）
    tls_keyfile: str = ""      # 服务端私钥（如 certs/server.key）
    tls_ca_certfile: str = ""  # CA 根证书（四端信任分发用，如 certs/ca.crt）

    wecom_webhook_url: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""

    feishu_webhook_url: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    voice_token: str = ""
    # LAN 直连 E2EE 密钥（32B base64 或明文 passphrase，SHA-256 派生；App VoiceCipher 对齐）
    voice_e2ee_key: str = ""
    # wake userSig 静态加密密钥（base64，解码后必须正好 32 字节 / AES-256）。
    # 由 KMS 或部署 Secret 注入；留空时 wake 签发 fail-closed 拒绝（不退化明文落库）。
    voice_user_sig_cipher_key: str = ""

    trtc_sdkappid: int = 0
    trtc_secretkey: str = ""
    trtc_room_prefix: str = "jax-"

    # SQLite is a development/test fixture only. Production must provide a
    # PostgreSQL URL and a wired PostgresVoiceStore adapter before startup.
    voice_storage_backend: str = "sqlite"
    voice_database_url: str = ""
    voice_db_path: str = str(PROJECT_ROOT / "backend" / "data" / "voice.db")
    # PostgreSQL 连接池（生产装配路径）。默认值必须非 0/非空；
    # app/voice/pg_storage.py 的 PostgresPoolConfig 会再校验一遍（含 max>=min）。
    voice_pg_pool_min_size: int = 1
    voice_pg_pool_max_size: int = 10
    voice_pg_connect_timeout_s: float = 5.0
    voice_pg_max_idle_s: float = 300.0
    voice_owner_credential: str = ""      # 本机 owner 凭证（配对码生成用）
    voice_sidecar_credential: str = ""    # 独立 sidecar 当前凭证（不得与 device 复用）
    voice_sidecar_credential_next: str = ""
    voice_sidecar_next_enabled_at: str = ""
    voice_sidecar_next_expires_at: str = ""
    voice_sidecar_config_revision: str = ""
    voice_production: bool = False        # 生产模式：缺 TLS/validator/限流/TRTC 任一拒绝启动
    voice_tls_enabled: bool = False       # 生产必须 TLS；http/ws 明文端点视为缺失
    # 方案 B ack 上报服务凭证（契约 brainService/rtcBridgeService；仅 .env）
    voice_brain_service_credential: str = ""   # Brain ingress/outbox 上报 brain_turns_sealed
    voice_rtc_bridge_credential: str = ""      # bounded rtc_bridge 上报 A3/A4
    voice_hello_private_key_pem: str = ""       # CP-only Ed25519 PKCS8 PEM
    voice_hello_public_key_pem: str = ""        # rtc_bridge redemption verifier PEM
    voice_rtc_bridge_cert_binding: str = ""     # trusted gateway derived certificate binding
    voice_gateway_shared_assertion: str = ""    # internal gateway assertion, never client-derived
    voice_trusted_gateway_hosts: str = "127.0.0.1,::1"  # 受信反向代理来源（云端边缘网关需显式声明）

    log_level: str = "INFO"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"配置文件缺失: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_monitors() -> MonitorsConfig:
    return MonitorsConfig.model_validate(_load_yaml("monitors.yaml"))


def load_detection() -> DetectionConfig:
    doc = _load_yaml("detection.yaml")
    return DetectionConfig.model_validate(doc["detection"])


def load_push() -> PushConfig:
    doc = _load_yaml("push.yaml")
    return PushConfig.model_validate(doc["push"])


def load_reminder() -> ReminderConfig:
    return ReminderConfig.model_validate(_load_yaml("detection.yaml")["reminder"])


def load_brain() -> BrainConfig:
    doc = _load_yaml("brain.yaml")
    return BrainConfig.model_validate(doc["brain"])


def load_settings() -> Settings:
    return Settings()


class AppConfig:
    """聚合配置对象（热重载时重建）"""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.monitors = load_monitors()
        self.detection = load_detection()
        self.push = load_push()
        self.reminder = load_reminder()
        self.brain = load_brain()

    def reload(self) -> list[str]:
        """热重载 config/*.yaml，返回错误清单（空 = 成功）"""
        errors: list[str] = []
        try:
            self.monitors = load_monitors()
        except Exception as e:  # noqa: BLE001
            errors.append(f"monitors.yaml: {e}")
        try:
            self.detection = load_detection()
            self.reminder = load_reminder()
        except Exception as e:  # noqa: BLE001
            errors.append(f"detection.yaml: {e}")
        try:
            self.push = load_push()
        except Exception as e:  # noqa: BLE001
            errors.append(f"push.yaml: {e}")
        try:
            self.brain = load_brain()
        except Exception as e:  # noqa: BLE001
            errors.append(f"brain.yaml: {e}")
        return errors


# 全局唯一配置实例
config = AppConfig()
