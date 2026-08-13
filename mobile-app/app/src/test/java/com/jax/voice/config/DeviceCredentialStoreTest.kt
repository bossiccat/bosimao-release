package com.jax.voice.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceCredentialStoreTest {

    @Test
    fun `credential vault persists ciphertext and round trips secret`() {
        val storage = RecordingCredentialStorage()
        val vault = DeviceCredentialVault(storage, FakeCredentialCipher())

        vault.save("device-123", "device-credential-secret")

        assertFalse(storage.ciphertext.orEmpty().contains("device-credential-secret"))
        assertEquals("device-123", vault.deviceId())
        assertEquals("device-credential-secret", vault.credential())

        storage.deviceId = "device-other"
        assertNull(vault.credential())
        assertTrue(storage.cleared)
    }

    @Test
    fun `voice config credential combines device id with secret while vault exposes secret only`() {
        val storage = RecordingCredentialStorage()
        val vault = DeviceCredentialVault(storage, FakeCredentialCipher())

        vault.save("device-123", "secret")

        assertEquals("secret", vault.credential())
        assertEquals("device-123.secret", VoiceConfig.deviceCredential(vault))
    }

    @Test
    fun `voice config session credential returns one vault snapshot for device and wire credential`() {
        val storage = RecordingCredentialStorage()
        val vault = DeviceCredentialVault(storage, FakeCredentialCipher())

        vault.save("device-123", "secret")

        val sessionCredential = VoiceConfig.deviceSessionCredential(vault)

        assertEquals("device-123", sessionCredential.deviceId)
        assertEquals("device-123.secret", sessionCredential.wireCredential)
        assertEquals(1, storage.loadCalls)
    }

    @Test
    fun `credential vault rejects blank registration and clears corrupt ciphertext`() {
        val storage = RecordingCredentialStorage()
        val vault = DeviceCredentialVault(storage, FakeCredentialCipher())

        assertTrue(runCatching { vault.save("device-123", " ") }.isFailure)
        storage.save("device-123", "invalid", "invalid")

        assertNull(vault.credential())
        assertTrue(storage.cleared)
    }

    private class RecordingCredentialStorage : CredentialStorage {
        var deviceId: String? = null
        var iv: String? = null
        var ciphertext: String? = null
        var cleared = false
        var loadCalls = 0

        override fun save(deviceId: String, iv: String, ciphertext: String) {
            this.deviceId = deviceId
            this.iv = iv
            this.ciphertext = ciphertext
            cleared = false
        }

        override fun load(): StoredCredential? {
            loadCalls += 1
            val currentDeviceId = deviceId ?: return null
            val currentIv = iv ?: return null
            val currentCiphertext = ciphertext ?: return null
            return StoredCredential(currentDeviceId, currentIv, currentCiphertext)
        }

        override fun clear() {
            deviceId = null
            iv = null
            ciphertext = null
            cleared = true
        }
    }

    private class FakeCredentialCipher : CredentialCipher {
        override fun encrypt(plaintext: ByteArray, associatedData: ByteArray): EncryptedCredential {
            val boundPlaintext = "${associatedData.decodeToString()}:${plaintext.decodeToString()}"
            return EncryptedCredential(
                iv = "test-iv".encodeToByteArray(),
                ciphertext = boundPlaintext.reversed().encodeToByteArray()
            )
        }

        override fun decrypt(
            encrypted: EncryptedCredential,
            associatedData: ByteArray
        ): ByteArray {
            val encoded = encrypted.ciphertext.decodeToString().reversed()
            val prefix = "${associatedData.decodeToString()}:"
            require(encoded.startsWith(prefix))
            return encoded.removePrefix(prefix).encodeToByteArray()
        }
    }
}
