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
    # 语音前台引擎：qwen_realtime 已通过真实 session.created 握手验证；apm 保留回退。
    voice_engine: str = "qwen"
    qwen_api_url: str = ""
    qwen_token: str = ""
    qwen_system_prompt: str = (
        "你是波斯猫的中文实时语音前台。只做自然对话、意图识别和协调工具调用。"
        "复杂任务必须调用 spawn_agent_thread，禁止在语音会话内拆解任务、读写文件或执行命令。"
        "后台任务用 agent_status 查询，用 steer_agent_thread 干预；需要风险确认时调用 approve_reply。"
        "不要输出工具名、JSON、内部状态或思维过程。默认简洁中文回答。"
    )
    # APM 会话（MiniCPM-o Realtime API）回退配置
    apm_api_url: str = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio"
    # 2026-08-23：撤销"不超过两句话"截断约束（用户实测智商受损、比小度差）——
    # 简短靠"先给结论+口语化"引导，复杂问题允许有信息量的完整回答
    apm_system_prompt: str = (
        "你是波斯猫，一个聪明务实的中文语音助手，性格友好、略带猫的俏皮。"
        "默认用口语化中文简洁回答（先给结论，一般两三句话）；"
        "用户问复杂问题时给出有信息量、有条理的回答，想深入再展开。"
        "不知道就诚实说不知道，不编造。"
        "\n\n【待命/唤醒规则】"
        "当用户说\"退下\"、\"你退下\"、\"波斯猫退下\"或类似的话让你离开时，"
        "你简短回应（如\"好的，我退下了\"），并在回复文本最末尾加上 [STANDBY] 标记。"
        "此后进入待命模式，不再回应任何用户的话，除非听到\"波斯猫\"三个字才恢复。"
        "当用户说\"波斯猫\"唤醒你时，你简短回应（如\"我在\"），"
        "并在回复文本最末尾加上 [ACTIVE] 标记，然后恢复正常对话。"
    )
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
    cfg.voice_engine = env.get("VOICE_ENGINE", cfg.voice_engine).strip().lower()
    cfg.qwen_api_url = env.get("QWEN_REALTIME_WS_URL", cfg.qwen_api_url)
    cfg.qwen_token = env.get("QWEN_REALTIME_API_KEY", cfg.qwen_token)
    cfg.qwen_system_prompt = env.get("QWEN_REALTIME_SYSTEM_PROMPT", cfg.qwen_system_prompt)
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
