package com.jax.voice.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceConfigMigrationTest {

    @Test
    fun `v2 migration preserves an existing session base url`() {
        val values = mutableMapOf<String, Any?>(
            "config_version" to 2,
            "session_base_url" to "https://voice.example.test"
        )

        val migrated = invokeMigration(values)

        assertEquals("https://voice.example.test", migrated["session_base_url"])
        assertEquals(3, migrated["config_version"])
    }

    @Test
    fun `legacy migration supplies defaults only for missing keys`() {
        val migrated = VoiceConfig.migratedValues(mutableMapOf("config_version" to 1))

        assertEquals("", migrated["session_base_url"])
        assertEquals(VoiceConfig.WAKE_DEFAULT_ENABLED, migrated["wake_enabled"])
        assertEquals(3, migrated["config_version"])
    }

    @Test
    fun `v3 migration is idempotent`() {
        val values = mutableMapOf<String, Any?>(
            "config_version" to 3,
            "session_base_url" to "https://voice.example.test",
            "wake_enabled" to true,
            "custom" to "kept"
        )

        val migrated = invokeMigration(values)

        assertTrue(migrated === values)
        assertEquals("https://voice.example.test", migrated["session_base_url"])
        assertFalse(migrated.keys.any { it.isBlank() })
    }

    @Suppress("UNCHECKED_CAST")
    private fun invokeMigration(values: MutableMap<String, Any?>): MutableMap<String, Any?> {
        val method = VoiceConfig::class.java.methods.firstOrNull {
            it.name == "migratedValues" && it.parameterTypes.contentEquals(arrayOf(MutableMap::class.java))
        }
        assertTrue("VoiceConfig 必须公开纯函数 migratedValues 以保证迁移可验证且保留已有值", method != null)
        return method!!.invoke(VoiceConfig, values) as MutableMap<String, Any?>
    }
}
