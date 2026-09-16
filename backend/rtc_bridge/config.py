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
    # 决策归位 M2 kill-switch：false = 本地能量 barge-in 与播放期上行门控整体退役，
    # 打断判定归位云端 smart_turn（声学+语义双检，非语义声音不触发）。
    # 默认 true 保持现有行为；G0 判定通过后切 false（run1 实锤：本地阈值在回声
    # 污染信号上误杀回复 0.5-1.5s——「没讲完」体验的直接根源）。
    local_barge_in: bool = True
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
    #
    # 上行 up_*（手机→桥，100 帧 / 1s）：保持「同机低延迟遥测」尺度 —— 迟到的上行帧
    # 确实没有价值，帧龄过期丢弃是对的。
    #
    # 下行 down_*（桥→手机）必须按「能装下一整段回复」的尺度配置，理由：
    #   模型把整段回复的音频以远快于实时的**突发**一次推下来（实测 6.88s 音频约 1.5s
    #   墙钟到齐），而 DownlinkShaper 严格按实时 50 帧/s 出队（shaper.py:137-138）。
    #   ⇒ 对下行而言「早到」的帧不是陈旧数据，恰恰是要播的内容。旧值 200 帧 / 1s 是按
    #   「同机低延迟遥测」调的，会把约 38% 的音频按**整帧**当陈旧数据丢弃（字被从中间
    #   切断），直接表现为「3 倍速 + 吐字不清 + 话讲不完」。
    #   1500 帧 × 20ms = 30s 仍是有界安全上界：超限照样丢旧 + 计数 + 打日志，只是把
    #   尺度从「1s 遥测」调到「整段回复」。
    up_max_frames: int = 100
    up_max_bytes: int = 100 * 640
    up_max_frame_age_ms: int = 1000
    down_max_frames: int = 1500
    down_max_bytes: int = 1500 * 640
    down_max_frame_age_ms: int = 30000
    # Control Plane hello redemption（HTTPS + mTLS，零重试）
    control_plane_base_url: str = ""
    control_plane_service_credential: str = ""
    control_plane_ca_file: str = ""
    control_plane_client_cert_file: str = ""
    control_plane_client_key_file: str = ""
    control_plane_gateway_assertion: str = ""
    # ⚠️ 这两个默认值曾经是 0.5 / 2.0 —— 那是「sidecar 与控制面同机」时代的错值。
    # 现在的控制面是**公网 HTTPS + mTLS**（`.run.tcloudbase.com`），TLS 建连本身就常 >0.5s，
    # 加上控制面冷启动与 nonce 落库，2.0s 总超时在稳态会偶发失败；而兑付失败是
    # **fail-closed 且终局**（server.py 发 `ctrl exit hello_redemption_failed`、永不建会话）
    # ⇒ 用户侧表现为「随机连不上」。drain 上报同样吃这两个值，超时即**静默丢失**（fire-once 无重试）。
    # 为什么一直没暴露：本地 `.env` 把两者覆盖成 2.0 / 10.0，而部署 env **未设**这两项 ⇒
    # 线上回落到这里的 0.5 / 2.0。改 redemption.py 的构造默认值是**无效**的 ——
    # 两个消费点（server.py `_redemption`、drain_ack.py `build_ack_reporter`）都显式传参。
    control_plane_connect_timeout_s: float = 5.0
    control_plane_total_timeout_s: float = 15.0
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

    def _float(name: str, default: float) -> float:
        try:
            return float(env.get(name, ""))
        except (TypeError, ValueError):
            return default

    cfg = BridgeConfig()
    cfg.ws_port = _int("RTC_BRIDGE_WS_PORT", cfg.ws_port)
    cfg.health_port = _int("RTC_BRIDGE_HEALTH_PORT", cfg.health_port)
    cfg.test_audio_enabled = str(
        env.get("RTC_BRIDGE_TEST_AUDIO_ENABLED", "")
    ).strip().lower() in {"1", "true", "yes"}
    cfg.voice_engine = env.get("VOICE_ENGINE", cfg.voice_engine).strip().lower()
    # M2 kill-switch（默认 true=本地决策；false=云端 smart_turn 决策归位）
    cfg.local_barge_in = str(
        env.get("RTC_BRIDGE_LOCAL_BARGE_IN", "true")
    ).strip().lower() not in {"0", "false", "no", "off"}
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
    cfg.control_plane_base_url = env.get("RTC_BRIDGE_CONTROL_PLANE_BASE_URL", "")
    cfg.control_plane_service_credential = env.get("RTC_BRIDGE_SERVICE_CREDENTIAL", "")
    cfg.control_plane_ca_file = env.get("RTC_BRIDGE_CONTROL_PLANE_CA_FILE", "")
    cfg.control_plane_client_cert_file = env.get("RTC_BRIDGE_CLIENT_CERT_FILE", "")
    cfg.control_plane_client_key_file = env.get("RTC_BRIDGE_CLIENT_KEY_FILE", "")
    cfg.control_plane_gateway_assertion = env.get("RTC_BRIDGE_GATEWAY_ASSERTION", "")
    cfg.control_plane_connect_timeout_s = _float(
        "RTC_BRIDGE_CONTROL_PLANE_CONNECT_TIMEOUT_S", cfg.control_plane_connect_timeout_s
    )
    cfg.control_plane_total_timeout_s = _float(
        "RTC_BRIDGE_CONTROL_PLANE_TOTAL_TIMEOUT_S", cfg.control_plane_total_timeout_s
    )
    return cfg
