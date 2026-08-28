package com.jax.voice.config

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.MessageDigest
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

enum class CredentialSlot {
    ACTIVE,
    STAGING,
    BACKUP
}

interface CredentialStorage {
    val supportsTransactions: Boolean
        get() = false

    fun save(deviceId: String, iv: String, ciphertext: String)
    fun load(): StoredCredential?
    fun clear()

    fun save(slot: CredentialSlot, credential: StoredCredential) {
        if (slot == CredentialSlot.ACTIVE) save(credential.deviceId, credential.iv, credential.ciphertext)
    }

    fun load(slot: CredentialSlot): StoredCredential? =
        if (slot == CredentialSlot.ACTIVE) load() else null

    fun clear(slot: CredentialSlot) {
        if (slot == CredentialSlot.ACTIVE) clear()
    }
}

interface CredentialCipher {
    fun encrypt(plaintext: ByteArray, associatedData: ByteArray): EncryptedCredential
    fun decrypt(encrypted: EncryptedCredential, associatedData: ByteArray): ByteArray
}

class DeviceCredentialVault(
    private val storage: CredentialStorage,
    private val cipher: CredentialCipher
) {
    init {
        if (storage.supportsTransactions) recover()
    }

    fun save(deviceId: String, credential: String) {
        require(deviceId.isNotBlank()) { "device_id cannot be blank" }
        require(credential.isNotBlank()) { "credential cannot be blank" }
        if (!storage.supportsTransactions) {
            saveActive(deviceId, credential)
            return
        }
        val encoded = encrypt(deviceId, credential)
        storage.save(CredentialSlot.STAGING, encoded)
        if (!sameValue(storage.load(CredentialSlot.STAGING), encoded)) return
        val active = storage.load(CredentialSlot.ACTIVE)
        if (active != null) {
            storage.save(CredentialSlot.BACKUP, active)
            if (!sameValue(storage.load(CredentialSlot.BACKUP), active)) return
        }
        storage.save(CredentialSlot.ACTIVE, encoded)
        if (!sameValue(storage.load(CredentialSlot.ACTIVE), encoded)) return
        storage.clear(CredentialSlot.BACKUP)
        storage.clear(CredentialSlot.STAGING)
    }

    private fun saveActive(deviceId: String, credential: String) {
        val encoded = encrypt(deviceId, credential)
        storage.save(
            deviceId = encoded.deviceId,
            iv = encoded.iv,
            ciphertext = encoded.ciphertext
        )
    }

    fun deviceId(): String? = snapshot()?.deviceId

    fun credential(): String? = snapshot()?.credential

    fun snapshot(): DeviceCredentialSnapshot? {
        val stored = storage.load(CredentialSlot.ACTIVE) ?: return null
        val snapshot = decrypt(stored)
        if (snapshot == null || snapshot.deviceId.isBlank()) {
            storage.clear(CredentialSlot.ACTIVE)
            return null
        }
        return snapshot
    }

    fun clear() {
        storage.clear(CredentialSlot.ACTIVE)
        storage.clear(CredentialSlot.STAGING)
        storage.clear(CredentialSlot.BACKUP)
    }

    private fun encrypt(deviceId: String, credential: String): StoredCredential {
        val encrypted = cipher.encrypt(credential.encodeToByteArray(), deviceId.encodeToByteArray())
        return StoredCredential(
            deviceId = deviceId,
            iv = Base64.getEncoder().encodeToString(encrypted.iv),
            ciphertext = Base64.getEncoder().encodeToString(encrypted.ciphertext)
        )
    }

    private fun decrypt(stored: StoredCredential): DeviceCredentialSnapshot? = try {
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
        null
    }

    private fun recover() {
        val active = storage.load(CredentialSlot.ACTIVE)
        val staging = storage.load(CredentialSlot.STAGING)
        val backup = storage.load(CredentialSlot.BACKUP)
        if (staging == null && backup == null) return
        if (active == null) return
        if (staging != null && backup == null) {
            when {
                sameValue(active, staging) ->
                    // 中断点：active 提升完成、staging 未清理 → 只补清理
                    storage.clear(CredentialSlot.STAGING)
                decrypt(staging) != null ->
                    // 中断点：staging 已写入并通过读回校验、backup/active 未动 → 完成提升。
                    // save() 在写 backup/active 之前必须先通过 staging 读回校验，
                    // 因此 staging 存在即代表新凭证完整可信，重启后补完事务而非丢弃。
                    promote(staging)
                else ->
                    // staging 无法解密（半写入/密钥轮换）→ 保留 active，丢弃损坏 staging
                    storage.clear(CredentialSlot.STAGING)
            }
            return
        }
        if (staging == null && backup != null) {
            if (sameValue(active, backup)) storage.clear(CredentialSlot.BACKUP)
            else restore(backup)
            return
        }
        if (staging != null && backup != null) {
            when {
                sameValue(active, staging) -> {
                    // 中断点：active==staging（提升完成）→ backup 是旧值残留，全部清理
                    storage.clear(CredentialSlot.BACKUP)
                    storage.clear(CredentialSlot.STAGING)
                }
                sameValue(active, backup) -> {
                    // 中断点：backup 写入后 active 被外部改动 → 以 active 为准清理事务槽
                    storage.clear(CredentialSlot.STAGING)
                    storage.clear(CredentialSlot.BACKUP)
                }
                else -> restore(backup)
            }
        }
    }

    /** 把已校验的 staging 提升为 active；写入失败保留全部槽位供下次恢复重试。 */
    private fun promote(staging: StoredCredential) {
        storage.save(CredentialSlot.ACTIVE, staging)
        if (sameValue(storage.load(CredentialSlot.ACTIVE), staging)) {
            storage.clear(CredentialSlot.STAGING)
        }
    }

    private fun restore(backup: StoredCredential) {
        storage.save(CredentialSlot.ACTIVE, backup)
        if (sameValue(storage.load(CredentialSlot.ACTIVE), backup)) {
            storage.clear(CredentialSlot.STAGING)
            storage.clear(CredentialSlot.BACKUP)
        }
    }

    private fun sameValue(left: StoredCredential?, right: StoredCredential?): Boolean {
        if (left == null || right == null) return false
        return MessageDigest.isEqual(left.deviceId.encodeToByteArray(), right.deviceId.encodeToByteArray()) &&
            MessageDigest.isEqual(left.iv.encodeToByteArray(), right.iv.encodeToByteArray()) &&
            MessageDigest.isEqual(left.ciphertext.encodeToByteArray(), right.ciphertext.encodeToByteArray())
    }
}

class SharedPreferencesCredentialStorage(context: Context) : CredentialStorage {
    private val preferences = context.applicationContext.getSharedPreferences(
        PREFS_NAME,
        Context.MODE_PRIVATE
    )

    override val supportsTransactions: Boolean = true

    override fun save(deviceId: String, iv: String, ciphertext: String) {
        save(CredentialSlot.ACTIVE, StoredCredential(deviceId, iv, ciphertext))
    }

    override fun save(slot: CredentialSlot, credential: StoredCredential) {
        preferences.edit()
            .putString(key(slot, KEY_DEVICE_ID), credential.deviceId)
            .putString(key(slot, KEY_IV), credential.iv)
            .putString(key(slot, KEY_CIPHERTEXT), credential.ciphertext)
            .commit()
    }

    override fun load(): StoredCredential? = load(CredentialSlot.ACTIVE)

    override fun load(slot: CredentialSlot): StoredCredential? {
        val deviceId = preferences.getString(key(slot, KEY_DEVICE_ID), null) ?: return null
        val iv = preferences.getString(key(slot, KEY_IV), null) ?: return null
        val ciphertext = preferences.getString(key(slot, KEY_CIPHERTEXT), null) ?: return null
        return StoredCredential(deviceId, iv, ciphertext)
    }

    override fun clear() = clear(CredentialSlot.ACTIVE)

    override fun clear(slot: CredentialSlot) {
        preferences.edit()
            .remove(key(slot, KEY_DEVICE_ID))
            .remove(key(slot, KEY_IV))
            .remove(key(slot, KEY_CIPHERTEXT))
            .commit()
    }

    private fun key(slot: CredentialSlot, suffix: String): String =
        "${slot.name.lowercase()}_$suffix"

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
