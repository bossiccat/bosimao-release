package com.jax.voice.voice

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceEntryAvailabilityTest {

    @Test
    fun `main talk entry is available even when listener service is stopped`() {
        val source = File(findSourceRoot(), "../MainActivity.kt").canonicalFile.readText()
        assertTrue(source.contains("VoiceEntry.startConversation(this, \"main\")"))
        assertFalse(source.contains("btnTalk.isEnabled = running"))
    }

    private fun findSourceRoot(): String {
        var dir: File? = File(System.getProperty("user.dir"))
        repeat(4) {
            val candidate = dir?.resolve("src/main/java/com/jax/voice/voice")
            if (candidate != null && candidate.isDirectory) return candidate.absolutePath
            dir = dir?.parentFile
        }
        error("source root not found")
    }
}
