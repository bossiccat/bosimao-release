package com.jax.voice.voice

/**
 * 统一 VoiceUiModel（SPEC §4.2 双层状态契约第二层 / DESIGN-DETAIL §2）。
 *
 * 体验状态只允许 10 个枚举值（跨端一致：Android 与 pet-ui 使用同名枚举）：
 * idle / requesting_permission / connecting / listening / endpointing /
 * thinking / speaking / interrupted / recovering / error。
 * UI 与服务只消费聚合模型，禁止用 isListening/isThinking/inCall/rtcExiting
 * 等并行业务布尔拼装 UI（SPEC §4.2）。
 */
enum class ExperienceState {
    IDLE, REQUESTING_PERMISSION, CONNECTING, LISTENING, ENDPOINTING,
    THINKING, SPEAKING, INTERRUPTED, RECOVERING, ERROR;

    companion object {
        /** 会话生命周期 → 默认体验状态（DESIGN-DETAIL §2.2；细化由上层事件驱动） */
        fun fromSession(state: VoiceSessionState, hasError: Boolean): ExperienceState = when {
            hasError -> ERROR
            state == VoiceSessionState.IDLE -> IDLE
            state == VoiceSessionState.SIGNING || state == VoiceSessionState.ENTERING -> CONNECTING
            state == VoiceSessionState.IN_ROOM -> LISTENING
            state == VoiceSessionState.EXITING -> CONNECTING // 文案"正在结束会话"
            else -> IDLE
        }

        /** 兼容旧六态映射（RtcClient onPhase / VoiceController 阶段） */
        fun fromPhase(phase: VoicePhase): ExperienceState = when (phase) {
            VoicePhase.IDLE, VoicePhase.MONITORING -> IDLE
            VoicePhase.LISTENING -> LISTENING
            VoicePhase.THINKING -> THINKING
            VoicePhase.SPEAKING -> SPEAKING
            VoicePhase.ALERTING -> ERROR
        }
    }
}

/** 合法主操作（DESIGN-DETAIL §2.1 主操作列；跨端一致） */
enum class VoiceAction {
    START, CANCEL, STOP_LISTENING, STOP_SPEAKING, RETRY,
    OPEN_SETTINGS, RE_PAIR, REBUILD_PLAYBACK, RECONNECT, TEXT_INPUT
}

/** 分类错误模型（SPEC §6 错误分类；UI 2 秒内展示原因与操作） */
data class VoiceError(
    val code: String,
    val message: String,
    val action: VoiceAction
)

/** 聚合 UI 模型：Android 与 Windows 唯一消费对象（SPEC §4.2） */
data class VoiceUiModel(
    val experience: ExperienceState = ExperienceState.IDLE,
    val session: VoiceSessionState = VoiceSessionState.IDLE,
    val transcript: String = "",
    val reply: String = "",
    val rms: Float = 0f,
    val error: VoiceError? = null
) {
    /** 当前主操作（由体验状态 + 错误类别决定，DESIGN-DETAIL §2.1） */
    val primaryAction: VoiceAction
        get() = when (experience) {
            ExperienceState.IDLE -> VoiceAction.START
            ExperienceState.REQUESTING_PERMISSION -> VoiceAction.OPEN_SETTINGS
            ExperienceState.CONNECTING -> VoiceAction.CANCEL
            ExperienceState.LISTENING -> VoiceAction.STOP_LISTENING
            ExperienceState.ENDPOINTING -> VoiceAction.CANCEL
            ExperienceState.THINKING -> VoiceAction.CANCEL
            ExperienceState.SPEAKING -> VoiceAction.STOP_SPEAKING
            ExperienceState.INTERRUPTED -> VoiceAction.CANCEL
            ExperienceState.RECOVERING -> VoiceAction.RETRY
            ExperienceState.ERROR -> error?.action ?: VoiceAction.RETRY
        }
}

/** 常见分类错误工厂（错误码与 SPEC §5 一致；message 为脱敏用户文案） */
object VoiceErrors {
    fun authFailed() = VoiceError("40101", "无法验证此设备，请重新配对", VoiceAction.RE_PAIR)
    fun revoked() = VoiceError("40103", "此设备已被撤销，当前会话已结束", VoiceAction.RE_PAIR)
    fun handshakeTimeout() = VoiceError("40801", "连接电脑超时", VoiceAction.RECONNECT)
    fun stateConflict() = VoiceError("40901", "上一个会话操作仍在处理", VoiceAction.RETRY)
    fun rateLimited() = VoiceError("42901", "请求过于频繁，请稍后重试", VoiceAction.RETRY)
    fun upstreamTimeout() = VoiceError("50401", "语音服务响应超时", VoiceAction.RETRY)
    fun micBusy() = VoiceError("mic_busy", "其他应用正在使用麦克风", VoiceAction.RETRY)
    fun playbackSilent() = VoiceError("playback_silent", "收到回复但未能播放", VoiceAction.REBUILD_PLAYBACK)
    fun sidecarDown() = VoiceError("sidecar_down", "电脑语音组件未运行", VoiceAction.RETRY)
}
