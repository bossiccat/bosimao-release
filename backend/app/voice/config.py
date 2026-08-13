"""voice 网关配置（mobile-voice-spec §8.4 + .env VOICE_TOKEN）

优先读 config/voice.yaml；文件缺失时用默认值（保证测试/无配置文件环境可跑）。
VOICE_TOKEN 仅从 .env 读取（Settings.voice_token），不入库、不进日志。
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .._frozen_paths import project_root

logger = logging.getLogger(__name__)

PROJECT_ROOT = project_root()
VOICE_YAML = PROJECT_ROOT / "config" / "voice.yaml"

# 默认模型目录（scripts/download_sherpa_models.py 下载目标，与 .gitignore 对齐）
DEFAULT_STT_MODEL_DIR = str(PROJECT_ROOT / "models" / "sherpa" / "wenetspeech-streaming")
MAX_SIDECAR_ROTATION_WINDOW_SECONDS = 600
_STATIC_HASH_PATTERN = re.compile(r"^jax-static-v1\$[0-9a-f]{64}$")


class SidecarCredentialConfigError(ValueError):
    """Sidecar credential 快照无效；异常不得包含 secret/hash。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_static_credential(secret: str) -> str:
    salt = "jax-static-v1"
    return f"{salt}${hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()}"


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SidecarCredentialConfigError("invalid sidecar rotation configuration") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SidecarCredentialHashSet:
    current_hash: str
    next_hash: str | None = None
    next_enabled_at: datetime | None = None
    next_expires_at: datetime | None = None
    config_revision: str = ""

    def validate(self) -> None:
        if not _STATIC_HASH_PATTERN.fullmatch(self.current_hash):
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        configured = (self.next_hash, self.next_enabled_at, self.next_expires_at)
        if all(value is None for value in configured):
            return
        if any(value is None for value in configured):
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        assert self.next_hash is not None
        assert self.next_enabled_at is not None
        assert self.next_expires_at is not None
        if not _STATIC_HASH_PATTERN.fullmatch(self.next_hash):
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        if self.current_hash == self.next_hash:
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        if any(value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value)
               for value in (self.next_enabled_at, self.next_expires_at)):
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        duration = (self.next_expires_at - self.next_enabled_at).total_seconds()
        if duration <= 0 or duration > MAX_SIDECAR_ROTATION_WINDOW_SECONDS:
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")

    def rotation_state(self, now: datetime) -> str:
        self.validate()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        if self.next_hash is None:
            return "rotation_inactive"
        assert self.next_enabled_at is not None and self.next_expires_at is not None
        if now < self.next_enabled_at:
            return "rotation_scheduled"
        if now >= self.next_expires_at:
            return "rotation_expired"
        return "rotation_active"


def build_sidecar_credential_hashes(
    *,
    current_secret: str,
    next_secret: str = "",
    next_enabled_at: str = "",
    next_expires_at: str = "",
    config_revision: str = "",
) -> SidecarCredentialHashSet:
    if not current_secret:
        raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
    if not next_secret:
        if next_enabled_at or next_expires_at:
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        result = SidecarCredentialHashSet(
            current_hash=_hash_static_credential(current_secret),
            config_revision=config_revision,
        )
    else:
        if not next_enabled_at or not next_expires_at or current_secret == next_secret:
            raise SidecarCredentialConfigError("invalid sidecar rotation configuration")
        result = SidecarCredentialHashSet(
            current_hash=_hash_static_credential(current_secret),
            next_hash=_hash_static_credential(next_secret),
            next_enabled_at=_parse_utc(next_enabled_at),
            next_expires_at=_parse_utc(next_expires_at),
            config_revision=config_revision,
        )
    result.validate()
    return result


class VoiceHalfDuplexConfig(BaseModel):
    stt: str = "sherpa-onnx-1.13.2"
    stt_model: str = "wenetspeech-streaming"
    stt_model_dir: str = DEFAULT_STT_MODEL_DIR
    tts: str = "edge-tts"
    edge_tts_voice: str = "zh-CN-XiaoxiaoNeural"
    edge_tts_cache_size: int = 10
    brain_trigger: list[str] = Field(
        default_factory=lambda: ["帮我", "拆解", "重构", "实现", "修", "写", "优化", "测试"]
    )


class VoiceSessionConfig(BaseModel):
    idle_timeout_s: float = 15.0          # V-5 静默回落
    max_round_ms: float = 60000.0         # 单轮上限
    hello_timeout_s: float = 10.0         # 等待 hello 超时
    heartbeat_interval_s: float = 30.0    # 心跳 30s
    heartbeat_timeout_s: float = 60.0     # 心跳超时踢连接（必须 > interval：发 ping 后立即检查 last_rx，
                                          # 若 timeout ≤ interval，被动应答客户端必被误踢——2026-08-05 现场 15<30）
    buffer_max_bytes: int = 10 * 1024 * 1024


class VoiceApmConfig(BaseModel):
    """M3 云端全双工（MiniCPM-o Realtime API，spec §8.2 云版）"""
    api_url: str = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio"
    system_prompt: str = "你是贾克斯，一个中文语音助手。回答简短自然，有问必答。"
    token: str = ""                    # 预留鉴权；当前 API 匿名可用（2026-08-05 实测）


class VoiceConfig(BaseModel):
    path: str = "auto"                    # auto|native|brain|apm（apm=云端全双工）
    half_duplex: VoiceHalfDuplexConfig = Field(default_factory=VoiceHalfDuplexConfig)
    session: VoiceSessionConfig = Field(default_factory=VoiceSessionConfig)
    apm: VoiceApmConfig = Field(default_factory=VoiceApmConfig)
    # token 由 .env VOICE_TOKEN 注入（不在 yaml 存凭据）
    token: str = ""
    require_token: bool = False           # VOICE_TOKEN 为空时自动放宽（开发态）
    # LAN 直连 E2EE（与 App VoiceCipher 对齐，AAD=seq8B / iv12+ct16 / SHA-256 派生）
    # 由 .env VOICE_E2EE_KEY 注入（32B base64 或明文 passphrase），不在 yaml 存凭据
    e2ee_key: bytes = b""
    e2ee_enabled: bool = False


class VoiceSecurityConfig(BaseModel):
    """商业语音生产安全配置（ADR-014 fail-closed；全部来自 .env，不落 yaml）"""

    production: bool = False
    tls_enabled: bool = False
    owner_credential_hash: str = ""
    sidecar_credential_hash: str = ""
    nonce_enabled: bool = True
    rate_limit_enabled: bool = True
    trtc_sdk_app_id: int = 0
    trtc_secret_key: str = ""
    rtc_termination_enabled: bool = False


class ProductionGateError(RuntimeError):
    """生产 fail-closed：缺少任一项必需能力时拒绝启动"""


def validate_production(security: VoiceSecurityConfig) -> list[str]:
    """返回缺失项清单；空列表 = 生产安全能力完备"""
    missing: list[str] = []
    if not security.tls_enabled:
        missing.append("tls_enabled")
    if not security.owner_credential_hash:
        missing.append("owner_credential_hash")
    if not security.sidecar_credential_hash:
        missing.append("sidecar_credential_hash")
    if not security.nonce_enabled:
        missing.append("nonce_enabled")
    if not security.rate_limit_enabled:
        missing.append("rate_limit_enabled")
    if not security.trtc_sdk_app_id:
        missing.append("trtc_sdk_app_id")
    if not security.trtc_secret_key:
        missing.append("trtc_secret_key")
    if security.production and not security.rtc_termination_enabled:
        missing.append("rtc_termination_enabled")
    return missing


def production_gate(security: VoiceSecurityConfig) -> list[str]:
    """运行时门禁：production=True 且缺项 → 抛 ProductionGateError（拒绝启动）"""
    missing = validate_production(security)
    if security.production and missing:
        raise ProductionGateError("生产安全能力缺失: " + ", ".join(missing))
    return missing


def runtime_missing(security: VoiceSecurityConfig) -> list[str]:
    """请求级 fail-closed 缺项清单：生产强制 TLS；开发模式豁免 TLS（本地 http），
    但 validator/nonce/限流/TRTC 等其余能力项一律强制，绝无匿名旁路。"""
    missing = validate_production(security)
    if not security.production and "tls_enabled" in missing:
        missing.remove("tls_enabled")
    return missing


def load_voice(token: str = "", e2ee_key_raw: str = "") -> VoiceConfig:
    """读取 config/voice.yaml + 注入 token/e2ee 密钥；yaml 缺失/损坏时回退默认"""
    cfg = VoiceConfig()
    if VOICE_YAML.exists():
        try:
            with VOICE_YAML.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            root = doc.get("voice", {})
            if isinstance(root, dict):
                cfg = VoiceConfig.model_validate(root)
        except Exception as e:  # noqa: BLE001 - 配置损坏不阻断启动
            logger.warning("voice.yaml 解析失败，使用默认配置: %s", e)
    cfg.token = token or ""
    if cfg.token:
        cfg.require_token = True
    raw_key = (e2ee_key_raw or "").strip()
    if raw_key:
        # 惰性导入：仅启用 E2EE 时才依赖 relay 包（同规则派生，保证互通）
        from relay.relay_protocol import load_e2ee_key

        cfg.e2ee_key = load_e2ee_key(raw_key)
        cfg.e2ee_enabled = True
    # 相对路径按项目根解析（与 CWD 无关）
    if not Path(cfg.half_duplex.stt_model_dir).is_absolute():
        cfg.half_duplex.stt_model_dir = str(
            PROJECT_ROOT / cfg.half_duplex.stt_model_dir
        )
    return cfg
