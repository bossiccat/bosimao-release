"""voice 网关配置（mobile-voice-spec §8.4 + .env VOICE_TOKEN）

优先读 config/voice.yaml；文件缺失时用默认值（保证测试/无配置文件环境可跑）。
VOICE_TOKEN 仅从 .env 读取（Settings.voice_token），不入库、不进日志。
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VOICE_YAML = PROJECT_ROOT / "config" / "voice.yaml"

# 默认模型目录（scripts/download_sherpa_models.py 下载目标，与 .gitignore 对齐）
DEFAULT_STT_MODEL_DIR = str(PROJECT_ROOT / "models" / "sherpa" / "wenetspeech-streaming")


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
