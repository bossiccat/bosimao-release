package com.jax.voice.net

import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Request
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class VoiceSessionApiTest {

    @Test
    fun `session request sends device bearer nonce and entry point`() {
        val requests = mutableListOf<Request>()
        val client = recordingClient(requests)
        val api = VoiceSessionApi(client) { "nonce-0123456789abcdef" }

        expectInterceptedRequest {
            api.fetchSession(
                baseUrl = "https://voice.example",
                deviceId = "device-123",
                credential = "device-123.device-credential",
                entryPoint = VoiceSessionApi.EntryPoint.OVERLAY
            )
        }

        val request = requests.single()
        assertEquals("Bearer device-123.device-credential", request.header("Authorization"))
        assertEquals("nonce-0123456789abcdef", request.header("X-Request-Nonce"))
        val body = checkNotNull(request.body).writeToUtf8()
        assertTrue(body.contains("\"device_id\":\"device-123\""))
        assertTrue(body.contains("\"entry_point\":\"overlay\""))
    }

    @Test
    fun `session request rejects credential subject mismatch before network`() {
        val requests = mutableListOf<Request>()
        val api = VoiceSessionApi(recordingClient(requests)) { "nonce-0123456789abcdef" }

        assertTrue(
            runCatching {
                api.fetchSession(
                    baseUrl = "https://voice.example",
                    deviceId = "device-123",
                    credential = "device-other.device-credential",
                    entryPoint = VoiceSessionApi.EntryPoint.MAIN
                )
            }.exceptionOrNull() is IllegalArgumentException
        )
        assertTrue(requests.isEmpty())
    }

    @Test
    fun `session request rejects credential without subject separator before network`() {
        val requests = mutableListOf<Request>()
        val api = VoiceSessionApi(recordingClient(requests)) { "nonce-0123456789abcdef" }

        assertTrue(
            runCatching {
                api.fetchSession(
                    baseUrl = "https://voice.example",
                    deviceId = "device-123",
                    credential = "device-123",
                    entryPoint = VoiceSessionApi.EntryPoint.MAIN
                )
            }.exceptionOrNull() is IllegalArgumentException
        )
        assertTrue(requests.isEmpty())
    }

    @Test
    fun `session request rejects whitespace-only secret before network`() {
        val requests = mutableListOf<Request>()
        val api = VoiceSessionApi(recordingClient(requests)) { "nonce-0123456789abcdef" }

        assertTrue(
            runCatching {
                api.fetchSession(
                    baseUrl = "https://voice.example",
                    deviceId = "device-123",
                    credential = "device-123.   ",
                    entryPoint = VoiceSessionApi.EntryPoint.MAIN
                )
            }.exceptionOrNull() is IllegalArgumentException
        )
        assertTrue(requests.isEmpty())
    }

    @Test
    fun `session request permits dots inside credential secret`() {
        val requests = mutableListOf<Request>()
        val api = VoiceSessionApi(recordingClient(requests)) { "nonce-0123456789abcdef" }

        expectInterceptedRequest {
            api.fetchSession(
                baseUrl = "https://voice.example",
                deviceId = "device-123",
                credential = "device-123.secret.part",
                entryPoint = VoiceSessionApi.EntryPoint.MAIN
            )
        }

        assertEquals("Bearer device-123.secret.part", requests.single().header("Authorization"))
    }

    @Test
    fun `session request obtains a fresh nonce for every call`() {
        val requests = mutableListOf<Request>()
        val nonces = ArrayDeque(
            listOf("nonce-aaaaaaaaaaaaaaaa", "nonce-bbbbbbbbbbbbbbbb")
        )
        val api = VoiceSessionApi(recordingClient(requests)) { nonces.removeFirst() }

        repeat(2) {
            expectInterceptedRequest {
                api.fetchSession(
                    baseUrl = "https://voice.example",
                    deviceId = "device-123",
                    credential = "device-123.device-credential",
                    entryPoint = VoiceSessionApi.EntryPoint.MAIN
                )
            }
        }

        val first = requests[0].header("X-Request-Nonce")
        val second = requests[1].header("X-Request-Nonce")
        assertEquals("nonce-aaaaaaaaaaaaaaaa", first)
        assertEquals("nonce-bbbbbbbbbbbbbbbb", second)
        assertNotEquals(first, second)
    }

    private fun recordingClient(requests: MutableList<Request>): OkHttpClient {
        return OkHttpClient.Builder()
            .addInterceptor(Interceptor { chain ->
                requests += chain.request()
                throw IOException(INTERCEPTED)
            })
            .build()
    }

    private fun expectInterceptedRequest(block: () -> Unit) {
        try {
            block()
            throw AssertionError("request was not intercepted")
        } catch (error: IOException) {
            assertEquals(INTERCEPTED, error.message)
        }
    }

    private fun okhttp3.RequestBody.writeToUtf8(): String {
        val buffer = okio.Buffer()
        writeTo(buffer)
        return buffer.readUtf8()
    }

    private companion object {
        const val INTERCEPTED = "request intercepted"
    }
}
