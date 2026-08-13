package com.jax.voice

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class SettingsPairingEntryTest {

    @Test
    fun `settings pairing entry disables duplicate submit and returns feedback on main thread`() {
        val source = File(findSourceRoot(), "SettingsActivity.kt").readText()

        assertTrue(source.contains("pairButton.isEnabled = false"))
        assertTrue(source.contains("pairingExecutor.execute"))
        assertTrue(source.contains("runOnUiThread"))
        assertTrue(source.contains("VoiceConfig.saveRegisteredDevice"))
        assertFalse(source.contains("setPairingCode"))
    }

    private fun findSourceRoot(): String {
        var dir: File? = File(System.getProperty("user.dir"))
        repeat(4) {
            val candidate = dir?.resolve("src/main/java/com/jax/voice")
            if (candidate != null && candidate.isDirectory) return candidate.absolutePath
            dir = dir?.parentFile
        }
        error("source root not found")
    }
}
