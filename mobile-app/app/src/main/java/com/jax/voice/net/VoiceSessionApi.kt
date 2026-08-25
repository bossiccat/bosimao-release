package com.jax.voice.net

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * 会话签发契约客户端（ADR-012 决策 #7 云函数代签 / ARCHITECTURE §3.4）。
 *
 * 契约（wire 层统一 snake_case，ADR-012 已定案）：
 *   POST {base_url}/api/v1/voice/session
 *   headers Authorization: Bearer <credential>, X-Request-Nonce: <nonce>
 *   req  { device_id, entry_point }
 *   resp { code:0, data:{ room_id:"jax-<device_id>", user_id:<device_id>, user_sig, sdk_app_id, session_id?, scene:"trtc_full_duplex" } }
 *
 * - base_url 由设置页 VoiceConfig.sessionBaseUrl 提供（默认留空 → 调用前校验提示填写）。
 * - userSig 短时效（≤600s）；SecretKey 唯一存云函数环境变量，本客户端不持有任何密钥。
 * - 用 OkHttp 同步 execute（阻塞）；调用方在后台协程执行（VoiceForegroundService scope）。
 */
class VoiceSessionApi(
    private val client: OkHttpClient = defaultClient,
    private val nonceProvider: () -> String = { java.util.UUID.randomUUID().toString() }
) {
    enum class EntryPoint(val wireValue: String) {
        MAIN("main"),
        OVERLAY("overlay"),
        NOTIFICATION("notification")
    }

    companion object {
        private const val TAG = "VoiceSessionApi"
        private const val TIMEOUT_SECONDS = 10L
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        val defaultClient: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .build()
    }

    /** 会话签发响应（snake_case → camelCase 映射；sessionId 按 OpenAPI 可选） */
    data class VoiceSession(
        val roomId: String,
        val userId: String,
        val userSig: String,
        val sdkAppId: Int,
        val scene: String,
        val sessionId: String? = null
    )

    /**
     * 终止原因（OpenAPI TerminateSessionRequest.reason 枚举；后端 pydantic Literal 校验，
     * extra="forbid"，多字段少字段都 400）。
     */
    enum class TerminateReason(val wireValue: String) {
        USER_STOP("user_stop"),
        REMOTE_LEAVE("remote_leave"),
        APP_SHUTDOWN("app_shutdown"),
        SECURITY_REVOKE("security_revoke"),
        ERROR_RECOVERY("error_recovery")
    }

    /**
     * terminate 请求体（POST /api/v1/voice/sessions/{session_id}/terminate）。
     * 契约：docs/api/commercial-voice-openapi.yaml TerminateSessionRequest +
     * backend/app/api/voice_termination_contract.py（extra=forbid，逐字段校验）。
     */
    data class TerminateRequest(
        val sessionId: String,
        val deviceId: String,
        val roomId: String,
        val generation: Long,
        /** 幂等键（uuid）；同键不同 payload → 后端 40912 */
        val requestId: String,
        val reason: TerminateReason,
        /** ISO-8601 date-time */
        val requestedAt: String
    )

    /**
     * 拉取 TRTC 进房凭证。失败抛 IOException（调用方 catch 后回落到监听态）。
     * @param baseUrl 云函数根地址，形如 https://<host>（可带路径前缀）；以 /api/v1/voice/session 拼接
     */
    @Throws(IOException::class)
    fun fetchSession(baseUrl: String, deviceId: String): VoiceSession {
        throw IllegalStateException(
            "secured session requires credential and explicit entry point: $baseUrl $deviceId"
        )
    }

    @Throws(IOException::class)
    fun fetchSession(
        baseUrl: String,
        deviceId: String,
        credential: String,
        entryPoint: EntryPoint
    ): VoiceSession {
        require(credential.isNotBlank()) { "credential 不能为空" }
        val separator = credential.indexOf('.')
        require(separator > 0) {
            "credential must contain a device_id subject"
        }
        val credentialSubject = credential.substring(0, separator)
        require(credentialSubject == deviceId) {
            "credential subject must match device_id"
        }
        val secret = credential.substring(separator + 1)
        require(secret.isNotBlank()) {
            "credential secret cannot be blank"
        }
        val nonce = nonceProvider().trim()
        require(nonce.isNotEmpty()) { "nonce 不能为空" }
        val base = baseUrl.trim().trimEnd('/')
        require(base.startsWith("https://")) {
            "base_url 必须为 https 地址（ADR-020 禁明文）"
        }
        val url = "$base/api/v1/voice/session"
        val bodyJson = "{\"device_id\":${jsonString(deviceId)}," +
            "\"entry_point\":${jsonString(entryPoint.wireValue)}}"
        val body = bodyJson.toRequestBody(JSON_MEDIA)
        val request = Request.Builder()
            .url(url)
            .post(body)
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer $credential")
            .header("X-Request-Nonce", nonce)
            .build()

        client.newCall(request).execute().use { resp ->
            val bodyText = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw IOException("HTTP ${resp.code}: $bodyText")
            }
            val json = try {
                JSONObject(bodyText)
            } catch (e: Exception) {
                throw IOException("响应非 JSON: ${e.message}", e)
            }
            val code = json.optInt("code", -1)
            if (code != 0) {
                throw IOException("业务码 $code: ${json.optString("message", "")}")
            }
            val data = json.optJSONObject("data")
                ?: throw IOException("响应缺 data")
            val roomId = data.optString("room_id").takeIf { it.isNotBlank() }
                ?: throw IOException("响应缺 room_id")
            val userSig = data.optString("user_sig").takeIf { it.isNotBlank() }
                ?: throw IOException("响应缺 user_sig")
            val sdkAppId = data.optInt("sdk_app_id", 0)
            if (sdkAppId <= 0) throw IOException("响应缺 sdk_app_id")
            return VoiceSession(
                roomId = roomId,
                userId = data.optString("user_id", deviceId),
                userSig = userSig,
                sdkAppId = sdkAppId,
                scene = data.optString("scene", "trtc_full_duplex"),
                sessionId = data.optString("session_id").takeIf { it.isNotBlank() }
            )
        }
    }

    /**
     * 发起受控终止（Task #23 上游接线）。fail-closed：
     * 非 202 / 业务码非 0 / 缺 data.termination_id 一律抛 IOException，调用方照常退房（CP 超时兜底已有）。
     *
     * 契约：POST {base_url}/api/v1/voice/sessions/{session_id}/terminate
     *   headers Authorization: Bearer <credential>, X-Request-Nonce: <nonce>（同 fetchSession :97-103 模式）
     *   req  { session_id, device_id, room_id, generation, request_id, reason, requested_at }（后端 extra=forbid）
     *   202  { code:0, data:{ termination_id, ... } }
     */
    @Throws(IOException::class)
    fun postTerminate(baseUrl: String, credential: String, request: TerminateRequest): String {
        require(request.sessionId.isNotBlank()) { "session_id 不能为空" }
        require(request.deviceId.isNotBlank()) { "device_id 不能为空" }
        require(request.roomId.isNotBlank()) { "room_id 不能为空" }
        require(request.generation >= 0) { "generation 必须非负" }
        require(request.requestId.isNotBlank()) { "request_id 不能为空" }
        require(request.requestedAt.isNotBlank()) { "requested_at 不能为空" }
        val separator = credential.indexOf('.')
        require(separator > 0) {
            "credential must contain a device_id subject"
        }
        val credentialSubject = credential.substring(0, separator)
        require(credentialSubject == request.deviceId) {
            "credential subject must match device_id"
        }
        require(credential.substring(separator + 1).isNotBlank()) {
            "credential secret cannot be blank"
        }
        val nonce = nonceProvider().trim()
        require(nonce.isNotEmpty()) { "nonce 不能为空" }
        val base = baseUrl.trim().trimEnd('/')
        require(base.startsWith("https://")) {
            "base_url 必须为 https 地址（ADR-020 禁明文）"
        }
        val url = "$base/api/v1/voice/sessions/${request.sessionId}/terminate"
        val bodyJson = "{\"session_id\":${jsonString(request.sessionId)}," +
            "\"device_id\":${jsonString(request.deviceId)}," +
            "\"room_id\":${jsonString(request.roomId)}," +
            "\"generation\":${request.generation}," +
            "\"request_id\":${jsonString(request.requestId)}," +
            "\"reason\":\"${request.reason.wireValue}\"," +
            "\"requested_at\":${jsonString(request.requestedAt)}}"
        val body = bodyJson.toRequestBody(JSON_MEDIA)
        val httpRequest = Request.Builder()
            .url(url)
            .post(body)
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer $credential")
            .header("X-Request-Nonce", nonce)
            .build()

        client.newCall(httpRequest).execute().use { resp ->
            val bodyText = resp.body?.string().orEmpty()
            if (resp.code != 202) {
                throw IOException("HTTP ${resp.code}: $bodyText")
            }
            val json = try {
                JSONObject(bodyText)
            } catch (e: Exception) {
                throw IOException("响应非 JSON: ${e.message}", e)
            }
            val code = json.optInt("code", -1)
            if (code != 0) {
                throw IOException("业务码 $code: ${json.optString("message", "")}")
            }
            val data = json.optJSONObject("data")
                ?: throw IOException("响应缺 data")
            return data.optString("termination_id").takeIf { it.isNotBlank() }
                ?: throw IOException("响应缺 termination_id")
        }
    }

    private fun jsonString(value: String): String {
        return buildString(value.length + 2) {
            append('"')
            value.forEach { char ->
                when (char) {
                    '"' -> append("\\\"")
                    '\\' -> append("\\\\")
                    '\b' -> append("\\b")
                    '\u000C' -> append("\\f")
                    '\n' -> append("\\n")
                    '\r' -> append("\\r")
                    '\t' -> append("\\t")
                    else -> if (char.code < 0x20) {
                        append("\\u%04x".format(char.code))
                    } else {
                        append(char)
                    }
                }
            }
            append('"')
        }
    }
}
