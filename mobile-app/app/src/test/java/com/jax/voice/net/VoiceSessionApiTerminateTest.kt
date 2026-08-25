package com.jax.voice.net

import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.ResponseBody.Companion.toResponseBody
import okhttp3.Response
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * VoiceSessionApi.postTerminate L0 单测（Task #23 上游接线，TDD）。
 *
 * 契约：POST /api/v1/voice/sessions/{session_id}/terminate（commercial-voice-openapi.yaml）；
 * 后端 TerminateSessionRequest extra=forbid；202 + code:0 + data.termination_id。
 * fail-closed：非 202 / 业务码非 0 / 缺 tid 一律抛 IOException。
 * 反作弊：新增测试，无 @Ignore/.only/弱化断言。
 */
class VoiceSessionApiTerminateTest {

    private val requests = mutableListOf<Request>()

    private fun apiWithResponse(code: Int, bodyText: String): VoiceSessionApi {
        val client = OkHttpClient.Builder()
            .addInterceptor(Interceptor { chain ->
                requests += chain.request()
                Response.Builder()
                    .request(chain.request())
                    .protocol(okhttp3.Protocol.HTTP_1_1)
                    .code(code)
                    .message("test")
                    .body(
                        bodyText.toResponseBody("application/json; charset=utf-8".toMediaType())
                    )
                    .build()
            })
            .build()
        return VoiceSessionApi(client) { "nonce-0123456789abcdef" }
    }

    private fun validRequest() = VoiceSessionApi.TerminateRequest(
        sessionId = "s-1",
        deviceId = "device-123",
        roomId = "jax-device-123",
        generation = 3L,
        requestId = "11111111-2222-3333-4444-555555555555",
        reason = VoiceSessionApi.TerminateReason.USER_STOP,
        requestedAt = "2026-02-25T10:00:00Z"
    )

    private fun postTerminate(api: VoiceSessionApi): String =
        api.postTerminate(
            baseUrl = "https://voice.example/",
            credential = "device-123.device-credential",
            request = validRequest()
        )

    // ---- T1: 202 成功 → 返回 termination_id，头与体符合契约 ----
    @Test
    fun `terminate posts bearer nonce and parses termination_id from 202`() {
        val api = apiWithResponse(
            202,
            """{"code":0,"data":{"termination_id":"tid-9","session_id":"s-1",
               "generation":3,"state":"TERMINATING"},"message":""}"""
        )
        assertEquals("tid-9", postTerminate(api))

        val request = requests.single()
        assertEquals(
            "https://voice.example/api/v1/voice/sessions/s-1/terminate",
            request.url.toString()
        )
        assertEquals("Bearer device-123.device-credential", request.header("Authorization"))
        assertEquals("nonce-0123456789abcdef", request.header("X-Request-Nonce"))
        assertEquals("application/json", request.header("Content-Type"))
        val body = checkNotNull(request.body).writeToUtf8()
        assertTrue(body.contains("\"session_id\":\"s-1\""))
        assertTrue(body.contains("\"device_id\":\"device-123\""))
        assertTrue(body.contains("\"room_id\":\"jax-device-123\""))
        assertTrue(body.contains("\"generation\":3"))
        assertTrue(body.contains("\"request_id\":\"11111111-2222-3333-4444-555555555555\""))
        assertTrue(body.contains("\"reason\":\"user_stop\""))
        assertTrue(body.contains("\"requested_at\":\"2026-02-25T10:00:00Z\""))
    }

    // ---- T2: 非 202 fail-closed ----
    @Test
    fun `terminate fails closed on non-202 status`() {
        val api = apiWithResponse(409, """{"code":40912,"data":null,"message":"conflict"}""")
        val error = runCatching { postTerminate(api) }.exceptionOrNull()
        assertTrue("非 202 必须抛 IOException", error is IOException)
        assertTrue(error!!.message!!.contains("409"))
    }

    // ---- T3: 业务码非 0 fail-closed ----
    @Test
    fun `terminate fails closed on non-zero business code`() {
        val api = apiWithResponse(202, """{"code":50301,"data":null,"message":"termination_unconfirmed"}""")
        val error = runCatching { postTerminate(api) }.exceptionOrNull()
        assertTrue(error is IOException)
        assertTrue(error!!.message!!.contains("50301"))
    }

    // ---- T4: 缺 termination_id fail-closed ----
    @Test
    fun `terminate fails closed when termination_id missing`() {
        val api = apiWithResponse(202, """{"code":0,"data":{"session_id":"s-1"},"message":""}""")
        assertTrue(runCatching { postTerminate(api) }.exceptionOrNull() is IOException)

        val empty = apiWithResponse(202, """{"code":0,"data":{"termination_id":""},"message":""}""")
        assertTrue(runCatching { postTerminate(empty) }.exceptionOrNull() is IOException)
    }

    // ---- T5: 非法入参在出网前拦截（fail-closed，零网络调用）----
    @Test
    fun `terminate rejects invalid input before network`() {
        for (transform in listOf(
            { r: VoiceSessionApi.TerminateRequest -> r.copy(sessionId = " ") },
            { r: VoiceSessionApi.TerminateRequest -> r.copy(deviceId = "") },
            { r: VoiceSessionApi.TerminateRequest -> r.copy(roomId = "") },
            { r: VoiceSessionApi.TerminateRequest -> r.copy(generation = -1L) },
            { r: VoiceSessionApi.TerminateRequest -> r.copy(requestId = "") },
            { r: VoiceSessionApi.TerminateRequest -> r.copy(requestedAt = "") },
            { r: VoiceSessionApi.TerminateRequest -> r.copy(deviceId = "device-other") } // subject 不匹配
        )) {
            val api = apiWithResponse(202, """{"code":0,"data":{"termination_id":"t"}}""")
            val error = runCatching {
                api.postTerminate(
                    baseUrl = "https://voice.example",
                    credential = "device-123.device-credential",
                    request = transform(validRequest())
                )
            }.exceptionOrNull()
            assertTrue(
                "非法入参必须 IllegalArgumentException: ${error?.message}",
                error is IllegalArgumentException
            )
        }
        assertTrue("非法入参不得发起网络请求", requests.isEmpty())
    }

    // ---- T6: base_url 非 https 出网前拦截 ----
    @Test
    fun `terminate rejects non-https base url before network`() {
        val api = apiWithResponse(202, """{"code":0,"data":{"termination_id":"t"}}""")
        val error = runCatching {
            api.postTerminate(
                baseUrl = "http://voice.example",
                credential = "device-123.device-credential",
                request = validRequest()
            )
        }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
        assertTrue(requests.isEmpty())
    }

    private fun okhttp3.RequestBody.writeToUtf8(): String {
        val buffer = okio.Buffer()
        writeTo(buffer)
        return buffer.readUtf8()
    }
}
