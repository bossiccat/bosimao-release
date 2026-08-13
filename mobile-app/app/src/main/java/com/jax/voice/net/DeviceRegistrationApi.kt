package com.jax.voice.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit

data class RegisteredDevice(
    val deviceId: String,
    val credentialId: String,
    val credentialSecret: String,
    val expiresAt: String
)

class DeviceRegistrationApi(
    private val client: OkHttpClient = defaultClient,
    private val nonceProvider: () -> String = { UUID.randomUUID().toString() }
) {
    @Throws(IOException::class)
    fun register(baseUrl: String, pairingCode: String, deviceName: String): RegisteredDevice {
        val base = baseUrl.trim().trimEnd('/')
        require(base.startsWith("https://")) {
            "base_url must be an HTTPS address (ADR-020 禁明文)"
        }
        val normalizedPairingCode = pairingCode.trim()
        require(normalizedPairingCode.length in PAIRING_CODE_LENGTH) {
            "pairing_code length must be within contract bounds"
        }
        val normalizedDeviceName = deviceName.trim()
        require(normalizedDeviceName.length in 1..DEVICE_NAME_MAX_LENGTH) {
            "device_name length must be within contract bounds"
        }
        val nonce = nonceProvider().trim()
        require(nonce.isNotBlank()) { "nonce cannot be blank" }

        val payload = JSONObject()
            .put("pairing_code", normalizedPairingCode)
            .put("device_name", normalizedDeviceName)
            .put("platform", "android")
            .toString()
            .toRequestBody(JSON_MEDIA)
        val request = Request.Builder()
            .url("$base/api/v1/voice/devices/register")
            .header("Content-Type", "application/json")
            .header("X-Request-Nonce", nonce)
            .post(payload)
            .build()

        client.newCall(request).execute().use { response ->
            val responseBody = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw IOException("device registration failed with HTTP ${response.code}")
            }
            val root = try {
                JSONObject(responseBody)
            } catch (error: Exception) {
                throw IOException("device registration returned invalid JSON", error)
            }
            val code = root.optInt("code", -1)
            if (code != 0) {
                throw IOException("device registration rejected with code $code")
            }
            val data = root.optJSONObject("data")
                ?: throw IOException("device registration response is missing data")
            return RegisteredDevice(
                deviceId = data.requiredString("device_id"),
                credentialId = data.requiredString("credential_id"),
                credentialSecret = data.requiredString(
                    "credential_secret",
                    CREDENTIAL_SECRET_LENGTH
                ),
                expiresAt = data.requiredString("expires_at")
            )
        }
    }

    private fun JSONObject.requiredString(
        name: String,
        allowedLength: IntRange = 1..Int.MAX_VALUE
    ): String {
        val normalized = optString(name).trim()
        return normalized.takeIf {
            it.isNotBlank() && it.length in allowedLength
        } ?: throw IOException("device registration response has invalid $name")
    }

    companion object {
        private const val TIMEOUT_SECONDS = 10L
        private val PAIRING_CODE_LENGTH = 20..256
        private const val DEVICE_NAME_MAX_LENGTH = 80
        private val CREDENTIAL_SECRET_LENGTH = 32..512
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
        private val defaultClient = OkHttpClient.Builder()
            .connectTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .build()
    }
}

class DevicePairingWorkflow(
    private val register: (String, String, String) -> RegisteredDevice,
    private val saveRegisteredDevice: (String, String) -> Unit
) {
    fun pair(baseUrl: String, pairingCode: String, deviceName: String): RegisteredDevice {
        val registered = register(baseUrl, pairingCode, deviceName)
        saveRegisteredDevice(registered.deviceId, registered.credentialSecret)
        return registered
    }
}
