package com.jax.voice.net

import org.junit.Assert.assertEquals
import org.junit.Test

class RtcAudioFrameRmsTest {
    @Test
    fun `captured voice activity is reported for loud local frame`() {
        val activities = mutableListOf<Float>()
        val rms = RtcAudioFrameRms(
            onRms = {},
            onVoiceActivity = { activities += it }
        )
        rms.processCapturedBytes(byteArrayOf(0x00, 0x40, 0x00, 0x40))

        assertEquals(1, activities.size)
    }
}
