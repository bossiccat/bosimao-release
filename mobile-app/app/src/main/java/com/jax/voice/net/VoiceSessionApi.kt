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
 *   req  { device_id }
 *   resp { code:0, data:{ room_id:"jax-<device_id>", user_id:<device_id>, user_sig, sdk_app_id, scene:"audio_call" } }
 *
 * - base_url 由设置页 VoiceConfig.sessionBaseUrl 提供（默认留空 → 调用前校验提示填写）。
 * - userSig 短时效（≤600s）；SecretKey 唯一存云函数环境变量，本客户端不持有任何密钥。
 * - 用 OkHttp 同步 execute（阻塞）；调用方在后台协程执行（VoiceForegroundService scope）。
 */
class VoiceSessionApi(
    private val client: OkHttpClient = defaultClient
) {
    companion object {
        private const val TAG = "VoiceSessionApi"
        private const val TIMEOUT_SECONDS = 10L
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        val defaultClient: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .build()
    }

    /** 会话签发响应（snake_case → camelCase 映射） */
    data class VoiceSession(
        val roomId: String,
        val userId: String,
        val userSig: String,
        val sdkAppId: Int,
        val scene: String
    )

    /**
     * 拉取 TRTC 进房凭证。失败抛 IOException（调用方 catch 后回落到监听态）。
     * @param baseUrl 云函数根地址，形如 https://<host>（可带路径前缀）；以 /api/v1/voice/session 拼接
     */
    @Throws(IOException::class)
    fun fetchSession(baseUrl: String, deviceId: String): VoiceSession {
        val base = baseUrl.trim().trimEnd('/')
        require(base.startsWith("http://") || base.startsWith("https://")) {
            "base_url 必须为 http(s) 地址"
        }
        val url = "$base/api/v1/voice/session"
        val body = JSONObject().put("device_id", deviceId).toString()
            .toRequestBody(JSON_MEDIA)
        val request = Request.Builder()
            .url(url)
            .post(body)
            .header("Content-Type", "application/json")
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
                scene = data.optString("scene", "audio_call")
            )
        }
    }
}
