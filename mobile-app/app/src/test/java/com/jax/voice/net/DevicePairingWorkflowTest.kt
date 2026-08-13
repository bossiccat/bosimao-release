package com.jax.voice.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class DevicePairingWorkflowTest {

    @Test
    fun `successful pairing saves registered device exactly once`() {
        val saves = mutableListOf<Pair<String, String>>()
        val expected = RegisteredDevice(
            deviceId = "device-123",
            credentialId = "credential-123",
            credentialSecret = "one-time-secret",
            expiresAt = "2026-08-09T00:00:00Z"
        )
        val workflow = DevicePairingWorkflow(
            register = { _, _, _ -> expected },
            saveRegisteredDevice = { deviceId, secret -> saves += deviceId to secret }
        )

        assertEquals(expected, workflow.pair("https://voice.example", "pair-code", "Jax Pixel"))
        assertEquals(listOf("device-123" to "one-time-secret"), saves)
    }

    @Test
    fun `failed pairing never overwrites an existing credential`() {
        var saveCalls = 0
        val workflow = DevicePairingWorkflow(
            register = { _, _, _ -> throw IOException("registration rejected") },
            saveRegisteredDevice = { _, _ -> saveCalls += 1 }
        )

        assertTrue(
            runCatching {
                workflow.pair("https://voice.example", "pair-code", "Jax Pixel")
            }.exceptionOrNull() is IOException
        )
        assertEquals(0, saveCalls)
    }
}
