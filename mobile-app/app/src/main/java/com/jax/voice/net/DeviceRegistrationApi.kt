package com.jax.voice.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.time.Instant
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
                expiresAt = data.requiredIsoTimestamp("expires_at")
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

    /**
     * expires_at 多形状解析（A6 修复，2026-08-21）：
     * - 首选形状：ISO8601 字符串（云端契约，devices.js toIso()，如 "2026-08-21T15:00:00.000Z"）
     * - 兜底形状：epoch 秒 number（如 1818856872）→ ×1000 转毫秒格式化为 ISO8601 UTC 字符串；
     *   下游（RegisteredDevice.expiresAt: String）类型不变。
     *
     * 判型说明：Android org.json 的 optString() 对 number 返回 ""，桌面 org.json（单测用）
     * 却隐式返回数字字符串——两套实现行为分叉，因此：
     * 1) isNull → 失败闭口（缺失或 JSON null）；
     * 2) optString 非空且为纯数字串（含负号）→ 视为 epoch（秒或毫秒，>1e12 按毫秒）
     *    统一转换并校验 >0（ISO8601 必含字母/连字符，纯数字串无歧义；同时兜住
     *    "后端把秒级时间戳当字符串发"的漂移形状）；
     * 3) 其余非空字符串 → ISO 契约形状原样直通；
     * 4) optLong > 0 → number 形状（Android optString 返回 "" 时走到这里）。
     */
    private fun JSONObject.requiredIsoTimestamp(name: String): String {
        if (isNull(name)) {
            throw IOException("device registration response has invalid $name")
        }
        val asText = optString(name).trim()
        if (asText.isNotEmpty()) {
            if (EPOCH_PATTERN.matches(asText)) {
                val epoch = asText.toLong()
                if (epoch <= 0L) {
                    throw IOException("device registration response has invalid $name")
                }
                return epochToIso(epoch)
            }
            return asText
        }
        val epoch = optLong(name, 0L)
        if (epoch <= 0L) {
            throw IOException("device registration response has invalid $name")
        }
        return epochToIso(epoch)
    }

    private fun epochToIso(epoch: Long): String {
        // >1e12 视为毫秒时间戳（秒级时间戳 2286 年前不可能超过该值）
        val millis = if (epoch > 1_000_000_000_000L) epoch else epoch * 1000L
        return Instant.ofEpochMilli(millis).toString()
    }

    companion object {
        private const val TIMEOUT_SECONDS = 10L
        private val PAIRING_CODE_LENGTH = 20..256
        private const val DEVICE_NAME_MAX_LENGTH = 80
        private val CREDENTIAL_SECRET_LENGTH = 32..512
        private val EPOCH_PATTERN = Regex("^-?\\d+$")
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
