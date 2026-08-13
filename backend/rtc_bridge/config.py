"""rtc_bridge 配置（独立进程，仅环境变量，禁硬编码凭据）

端口：sidecar WS :19092（127.0.0.1 不对外）、健康检查 HTTP :19093。
APM（MiniCPM-o）默认值与 backend/app/voice/apm_bridge.py 对齐，可经环境变量覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class BridgeConfig:
    ws_host: str = "127.0.0.1"
    ws_port: int = 19092
    health_host: str = "127.0.0.1"
    health_port: int = 19093
    test_audio_enabled: bool = False
    # APM 会话（MiniCPM-o Realtime API）
    apm_api_url: str = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio"
    apm_system_prompt: str = "你是贾克斯，一个中文语音助手。回答简短自然，有问必答。"
    apm_token: str = ""
    # 下行整形：帧长（ms）/ 采样率（全链路 16k s16）
    down_frame_ms: int = 20
    sample_rate: int = 16000
    # 有界队列预算（AC-10：帧数/字节/帧龄三约束；压力测试后可调）
    up_max_frames: int = 100
    up_max_bytes: int = 100 * 640
    up_max_frame_age_ms: int = 1000
    down_max_frames: int = 200
    down_max_bytes: int = 200 * 640
    down_max_frame_age_ms: int = 1000
    # 会话保护
    no_peer_timeout_s: float = 120.0   # 进房后长时间无远端加入 → 退房回待命
    extra: dict = field(default_factory=dict)


def load_bridge_config(env: dict | None = None) -> BridgeConfig:
    """从环境变量加载；env 可注入（测试用）"""
    env = env if env is not None else os.environ

    def _int(name: str, default: int) -> int:
        try:
            return int(env.get(name, ""))
        except (TypeError, ValueError):
            return default

    cfg = BridgeConfig()
    cfg.ws_port = _int("RTC_BRIDGE_WS_PORT", cfg.ws_port)
    cfg.health_port = _int("RTC_BRIDGE_HEALTH_PORT", cfg.health_port)
    cfg.test_audio_enabled = str(
        env.get("RTC_BRIDGE_TEST_AUDIO_ENABLED", "")
    ).strip().lower() in {"1", "true", "yes"}
    cfg.apm_api_url = env.get("APM_API_URL", cfg.apm_api_url)
    cfg.apm_system_prompt = env.get("APM_SYSTEM_PROMPT", cfg.apm_system_prompt)
    cfg.apm_token = env.get("APM_TOKEN", cfg.apm_token)
    cfg.down_frame_ms = _int("RTC_BRIDGE_DOWN_FRAME_MS", cfg.down_frame_ms)
    # 有界队列预算（AC-10）
    cfg.up_max_frames = _int("RTC_BRIDGE_UP_MAX_FRAMES", cfg.up_max_frames)
    cfg.up_max_bytes = _int("RTC_BRIDGE_UP_MAX_BYTES", cfg.up_max_bytes)
    cfg.up_max_frame_age_ms = _int("RTC_BRIDGE_UP_MAX_FRAME_AGE_MS", cfg.up_max_frame_age_ms)
    cfg.down_max_frames = _int("RTC_BRIDGE_DOWN_MAX_FRAMES", cfg.down_max_frames)
    cfg.down_max_bytes = _int("RTC_BRIDGE_DOWN_MAX_BYTES", cfg.down_max_bytes)
    cfg.down_max_frame_age_ms = _int("RTC_BRIDGE_DOWN_MAX_FRAME_AGE_MS", cfg.down_max_frame_age_ms)
    return cfg
