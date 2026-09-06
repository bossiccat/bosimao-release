package com.jax.voice.voice

import android.os.SystemClock

/**
 * 统一用户打断入口：把点击和本地语音活动都送入同一个幂等控制器。
 * 该类不持有 RTC 状态，只负责装配可测试的事件边界。
 */
class VoiceBargeInWiring(
    interruptPlayback: () -> Unit,
    onExperience: (ExperienceState) -> Unit,
    /** 单调时钟注入（BargeInController 播放段边界与起始保护窗计时；测试可伪造） */
    elapsedMs: () -> Long = { SystemClock.elapsedRealtime() }
) {
    private val controller = BargeInController(
        interruptPlayback = interruptPlayback,
        onExperience = onExperience,
        elapsedMs = elapsedMs
    )

    fun onExperience(state: ExperienceState) {
        controller.onExperienceChange(state)
    }

    fun onTap() {
        controller.interrupt("tap")
    }

    fun onUserVoiceActivity() {
        controller.interrupt("user_voice")
    }

    fun flush() {
        controller.flush()
    }

    val interruptGeneration: Int
        get() = controller.interruptGeneration
}
