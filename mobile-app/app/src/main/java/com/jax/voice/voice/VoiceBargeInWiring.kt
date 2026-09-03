package com.jax.voice.voice

/**
 * 统一用户打断入口：把点击和本地语音活动都送入同一个幂等控制器。
 * 该类不持有 RTC 状态，只负责装配可测试的事件边界。
 */
class VoiceBargeInWiring(
    interruptPlayback: () -> Unit,
    onExperience: (ExperienceState) -> Unit
) {
    private val controller = BargeInController(
        interruptPlayback = interruptPlayback,
        onExperience = onExperience
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
