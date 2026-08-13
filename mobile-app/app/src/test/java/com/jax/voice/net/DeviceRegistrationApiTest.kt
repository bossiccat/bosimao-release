package com.jax.voice.net

import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class DeviceRegistrationApiTest {

    @Test
    fun `register sends pairing payload to contract path with a fresh nonce`() {
        val requests = mutableListOf<Request>()
        val nonces = ArrayDeque(listOf("nonce-aaaaaaaaaaaaaaaa", "nonce-bbbbbbbbbbbbbbbb"))
        val api = DeviceRegistrationApi(successClient(requests)) { nonces.removeFirst() }

        repeat(2) {
            api.register("https://voice.example/", VALID_PAIRING_CODE, "Jax Pixel")
        }

        assertEquals(2, requests.size)
        assertEquals("/api/v1/voice/devices/register", requests[0].url.encodedPath)
        val body = checkNotNull(requests[0].body).writeToUtf8()
        assertTrue(body.contains("\"pairing_code\":\"$VALID_PAIRING_CODE\""))
        assertTrue(body.contains("\"device_name\":\"Jax Pixel\""))
        assertTrue(body.contains("\"platform\":\"android\""))
        assertEquals("nonce-aaaaaaaaaaaaaaaa", requests[0].header("X-Request-Nonce"))
        assertEquals("nonce-bbbbbbbbbbbbbbbb", requests[1].header("X-Request-Nonce"))
        assertNotEquals(
            requests[0].header("X-Request-Nonce"),
            requests[1].header("X-Request-Nonce")
        )
    }

    @Test
    fun `register parses every required one-time credential field`() {
        val result = DeviceRegistrationApi(successClient()).register(
            "https://voice.example",
            VALID_PAIRING_CODE,
            "Jax Pixel"
        )

        assertEquals("device-123", result.deviceId)
        assertEquals("credential-123", result.credentialId)
        assertEquals("secret-with-at-least-thirty-two-characters", result.credentialSecret)
        assertEquals("2026-08-09T00:00:00Z", result.expiresAt)
    }

    @Test
    fun `register normalizes surrounding whitespace from required response fields`() {
        val paddedBody = successBody()
            .replace("device-123", "  device-123  ")
            .replace("credential-123", "  credential-123  ")
            .replace(
                "secret-with-at-least-thirty-two-characters",
                "  secret-with-at-least-thirty-two-characters  "
            )
            .replace("2026-08-09T00:00:00Z", "  2026-08-09T00:00:00Z  ")
        val api = DeviceRegistrationApi(clientReturning(response(201, paddedBody))) {
            "nonce-0123456789abcdef"
        }

        val result = api.register("https://voice.example", VALID_PAIRING_CODE, "Jax Pixel")

        assertEquals("device-123", result.deviceId)
        assertEquals("credential-123", result.credentialId)
        assertEquals("secret-with-at-least-thirty-two-characters", result.credentialSecret)
        assertEquals("2026-08-09T00:00:00Z", result.expiresAt)
    }

    @Test
    fun `register fails closed on transport business and incomplete responses`() {
        val failures = listOf(
            response(401, errorBody(40101)),
            response(201, errorBody(40102)),
            response(201, successBody().replace("\"device_id\":\"device-123\",", "")),
            response(201, successBody().replace("\"credential_id\":\"credential-123\",", "")),
            response(
                201,
                successBody().replace(
                    "\"credential_secret\":\"secret-with-at-least-thirty-two-characters\",",
                    ""
                )
            ),
            response(201, successBody().replace("\"expires_at\":\"2026-08-09T00:00:00Z\"", ""))
        )

        failures.forEach { failedResponse ->
            val api = DeviceRegistrationApi(clientReturning(failedResponse)) {
                "nonce-0123456789abcdef"
            }
            assertTrue(
                "response should fail closed: ${failedResponse.code}",
                runCatching {
                    api.register("https://voice.example", VALID_PAIRING_CODE, "Jax Pixel")
                }.exceptionOrNull() is IOException
            )
        }
    }

    @Test
    fun `register rejects whitespace-only pairing code before network`() {
        assertInvalidInput(" ".repeat(20), "Jax Pixel")
    }

    @Test
    fun `register rejects pairing code shorter than contract minimum before network`() {
        assertInvalidInput("short", "Jax Pixel")
    }

    @Test
    fun `register rejects pairing code longer than contract maximum before network`() {
        assertInvalidInput("p".repeat(257), "Jax Pixel")
    }

    @Test
    fun `register rejects device name longer than contract maximum before network`() {
        assertInvalidInput(VALID_PAIRING_CODE, "d".repeat(81))
    }

    @Test
    fun `register rejects whitespace-only required response fields`() {
        val invalidBodies = listOf(
            successBody().replace("device-123", " "),
            successBody().replace("credential-123", " "),
            successBody().replace("secret-with-at-least-thirty-two-characters", " ".repeat(32)),
            successBody().replace("2026-08-09T00:00:00Z", " ")
        )

        invalidBodies.forEach { body ->
            val api = DeviceRegistrationApi(clientReturning(response(201, body))) {
                "nonce-0123456789abcdef"
            }
            assertTrue(
                runCatching {
                    api.register("https://voice.example", VALID_PAIRING_CODE, "Jax Pixel")
                }.exceptionOrNull() is IOException
            )
        }
    }

    @Test
    fun `register rejects credential secret shorter than contract minimum`() {
        assertInvalidCredentialSecret("s".repeat(31))
    }

    @Test
    fun `register rejects credential secret longer than contract maximum`() {
        assertInvalidCredentialSecret("s".repeat(513))
    }

    private fun assertInvalidInput(pairingCode: String, deviceName: String) {
        val requests = mutableListOf<Request>()
        val api = DeviceRegistrationApi(successClient(requests)) { "nonce-0123456789abcdef" }

        assertTrue(
            runCatching {
                api.register("https://voice.example", pairingCode, deviceName)
            }.exceptionOrNull() is IllegalArgumentException
        )
        assertTrue(requests.isEmpty())
    }

    private fun assertInvalidCredentialSecret(invalidSecret: String) {
        val body = successBody().replace(
            "secret-with-at-least-thirty-two-characters",
            invalidSecret
        )
        val api = DeviceRegistrationApi(clientReturning(response(201, body))) {
            "nonce-0123456789abcdef"
        }

        assertTrue(
            runCatching {
                api.register("https://voice.example", VALID_PAIRING_CODE, "Jax Pixel")
            }.exceptionOrNull() is IOException
        )
    }

    private fun successClient(requests: MutableList<Request> = mutableListOf()): OkHttpClient {
        return OkHttpClient.Builder()
            .addInterceptor(Interceptor { chain ->
                requests += chain.request()
                response(201, successBody(), chain.request())
            })
            .build()
    }

    private fun clientReturning(response: Response): OkHttpClient {
        return OkHttpClient.Builder()
            .addInterceptor(Interceptor { chain ->
                response.newBuilder().request(chain.request()).build()
            })
            .build()
    }

    private fun response(
        code: Int,
        body: String,
        request: Request = Request.Builder().url("https://voice.example").build()
    ): Response {
        return Response.Builder()
            .request(request)
            .protocol(Protocol.HTTP_1_1)
            .code(code)
            .message("test")
            .body(body.toResponseBody())
            .build()
    }

    private fun successBody(): String =
        """{"code":0,"data":{"device_id":"device-123","credential_id":"credential-123","credential_secret":"secret-with-at-least-thirty-two-characters","expires_at":"2026-08-09T00:00:00Z"},"message":""}"""

    private fun errorBody(code: Int): String =
        """{"code":$code,"data":null,"message":"auth_failed"}"""

    private companion object {
        const val VALID_PAIRING_CODE = "pair-code-0123456789ab"
    }

    private fun okhttp3.RequestBody.writeToUtf8(): String {
        val buffer = okio.Buffer()
        writeTo(buffer)
        return buffer.readUtf8()
    }
}
