package com.jax.voice.voice

import org.junit.Assert.assertEquals
import org.junit.Test

class VoiceBargeInWiringTest {
    @Test
    fun `speaking user voice interrupts playback and returns to listening`() {
        val calls = mutableListOf<String>()
        val experiences = mutableListOf<ExperienceState>()
        // 伪造单调时钟：建模「已播放一段时间」（越过 BargeInController 的播放起始保护窗）。
        // 用默认 SystemClock 时 JVM 单测拿到常量 0，保护窗会把首次语音打断一并挡掉。
        var elapsed = 0L
        val wiring = VoiceBargeInWiring(
            interruptPlayback = { calls += "interrupt" },
            onExperience = { experiences += it },
            elapsedMs = { elapsed }
        )

        wiring.onExperience(ExperienceState.SPEAKING) // 播放段起始 elapsed=0
        elapsed = 1_000L // 已播 1s，越过 400ms 起始保护窗
        wiring.onUserVoiceActivity()

        assertEquals(listOf("interrupt"), calls)
        assertEquals(
            listOf(ExperienceState.INTERRUPTED, ExperienceState.LISTENING),
            experiences
        )
    }

    @Test
    fun `tap while speaking uses same idempotent interrupt path`() {
        var calls = 0
        val wiring = VoiceBargeInWiring(
            interruptPlayback = { calls++ },
            onExperience = {}
        )

        wiring.onExperience(ExperienceState.SPEAKING)
        wiring.onTap()
        wiring.onTap()

        assertEquals(1, calls)
    }
}
