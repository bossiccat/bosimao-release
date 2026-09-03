package com.jax.voice.voice

import org.junit.Assert.assertEquals
import org.junit.Test

class VoiceBargeInWiringTest {
    @Test
    fun `speaking user voice interrupts playback and returns to listening`() {
        val calls = mutableListOf<String>()
        val experiences = mutableListOf<ExperienceState>()
        val wiring = VoiceBargeInWiring(
            interruptPlayback = { calls += "interrupt" },
            onExperience = { experiences += it }
        )

        wiring.onExperience(ExperienceState.SPEAKING)
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
