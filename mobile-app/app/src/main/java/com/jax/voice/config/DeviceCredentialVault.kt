package com.jax.voice.config

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class StoredCredential(
    val deviceId: String,
    val iv: String,
    val ciphertext: String
)

data class EncryptedCredential(
    val iv: ByteArray,
    val ciphertext: ByteArray
)

data class DeviceCredentialSnapshot(
    val deviceId: String,
    val credential: String
)

interface CredentialStorage {
    fun save(deviceId: String, iv: String, ciphertext: String)
    fun load(): StoredCredential?
    fun clear()
}

interface CredentialCipher {
    fun encrypt(plaintext: ByteArray, associatedData: ByteArray): EncryptedCredential
    fun decrypt(encrypted: EncryptedCredential, associatedData: ByteArray): ByteArray
}

class DeviceCredentialVault(
    private val storage: CredentialStorage,
    private val cipher: CredentialCipher
) {
    fun save(deviceId: String, credential: String) {
        require(deviceId.isNotBlank()) { "device_id cannot be blank" }
        require(credential.isNotBlank()) { "credential cannot be blank" }
        val associatedData = deviceId.encodeToByteArray()
        val encrypted = cipher.encrypt(credential.encodeToByteArray(), associatedData)
        storage.save(
            deviceId = deviceId,
            iv = Base64.getEncoder().encodeToString(encrypted.iv),
            ciphertext = Base64.getEncoder().encodeToString(encrypted.ciphertext)
        )
    }

    fun deviceId(): String? = storage.load()?.deviceId?.takeIf { it.isNotBlank() }

    fun credential(): String? = snapshot()?.credential

    fun snapshot(): DeviceCredentialSnapshot? {
        val stored = storage.load() ?: return null
        return try {
            val encrypted = EncryptedCredential(
                iv = Base64.getDecoder().decode(stored.iv),
                ciphertext = Base64.getDecoder().decode(stored.ciphertext)
            )
            val credential = cipher.decrypt(encrypted, stored.deviceId.encodeToByteArray())
                .decodeToString()
                .takeIf { it.isNotBlank() }
                ?: throw IllegalStateException("decrypted credential is blank")
            DeviceCredentialSnapshot(stored.deviceId, credential)
        } catch (_: Exception) {
            storage.clear()
            null
        }
    }

    fun clear() = storage.clear()
}

class SharedPreferencesCredentialStorage(context: Context) : CredentialStorage {
    private val preferences = context.applicationContext.getSharedPreferences(
        PREFS_NAME,
        Context.MODE_PRIVATE
    )

    override fun save(deviceId: String, iv: String, ciphertext: String) {
        preferences.edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_IV, iv)
            .putString(KEY_CIPHERTEXT, ciphertext)
            .apply()
    }

    override fun load(): StoredCredential? {
        val deviceId = preferences.getString(KEY_DEVICE_ID, null) ?: return null
        val iv = preferences.getString(KEY_IV, null) ?: return null
        val ciphertext = preferences.getString(KEY_CIPHERTEXT, null) ?: return null
        return StoredCredential(deviceId, iv, ciphertext)
    }

    override fun clear() {
        preferences.edit()
            .remove(KEY_DEVICE_ID)
            .remove(KEY_IV)
            .remove(KEY_CIPHERTEXT)
            .apply()
    }

    private companion object {
        const val PREFS_NAME = "jax_voice_device_credential"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_IV = "credential_iv"
        const val KEY_CIPHERTEXT = "credential_ciphertext"
    }
}

class AndroidKeystoreCredentialCipher : CredentialCipher {
    override fun encrypt(plaintext: ByteArray, associatedData: ByteArray): EncryptedCredential {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        cipher.updateAAD(associatedData)
        return EncryptedCredential(cipher.iv, cipher.doFinal(plaintext))
    }

    override fun decrypt(
        encrypted: EncryptedCredential,
        associatedData: ByteArray
    ): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_BITS, encrypted.iv))
        cipher.updateAAD(associatedData)
        return cipher.doFinal(encrypted.ciphertext)
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE_PROVIDER).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE_PROVIDER)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }

    private companion object {
        const val KEYSTORE_PROVIDER = "AndroidKeyStore"
        const val KEY_ALIAS = "jax_voice_device_credential_v1"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val GCM_TAG_BITS = 128
    }
}
