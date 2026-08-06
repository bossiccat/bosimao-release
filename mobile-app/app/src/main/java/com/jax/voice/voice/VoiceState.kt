package com.jax.voice.voice

/**
 * 手机端六态（对齐 PRD pet_state / pet-ui petMachine，spec §4.6）
 *
 * monitoring ──唤醒词命中──► listening ──VAD语音结束──► thinking ──TTS开始──► speaking
 *     ▲                          │  ▲                        │                    │
 *     └────────静默超时(15s)────────┘  │(barge-in <500ms)       │(TTS 结束)          │
 *     ◄───────────────────────────────┴────────────────────────┴──────────────────┘
 */
enum class VoicePhase {
    IDLE, MONITORING, LISTENING, THINKING, SPEAKING, ALERTING
}

/** WS 连接状态（spec §7.4 断线重连：指数退避 1s→2s→4s→…→30s） */
enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED }

/** 服务是否在跑（前台服务 + 常驻麦克风） */
enum class ServiceState { STOPPED, RUNNING }

/** UI 订阅的唯一数据模型（由 VoiceController 单例 StateFlow 驱动） */
data class VoiceUiState(
    val phase: VoicePhase = VoicePhase.IDLE,
    val connection: ConnectionState = ConnectionState.DISCONNECTED,
    val service: ServiceState = ServiceState.STOPPED,
    val wakeCount: Int = 0,
    val lastKeyword: String = "",
    val rms: Float = 0f,
    val wakeEnabled: Boolean = true,
    val threshold: Float = 0.25f,
    /** 最近一次连接/配对失败原因（v0.4.7 诊断：UI 直接显示，方便用户报错） */
    val lastError: String = ""
)
