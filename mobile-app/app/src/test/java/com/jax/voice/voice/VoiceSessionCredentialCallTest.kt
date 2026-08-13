package com.jax.voice.voice

import com.jax.voice.net.VoiceSessionApi
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

class VoiceSessionCredentialCallTest {

    @Test
    fun `p0 source maps to secured session entry point`() {
        assertEquals(VoiceSessionApi.EntryPoint.MAIN, sessionEntryPoint("main"))
        assertEquals(VoiceSessionApi.EntryPoint.OVERLAY, sessionEntryPoint("overlay"))
        assertEquals(VoiceSessionApi.EntryPoint.NOTIFICATION, sessionEntryPoint("notification"))
        assertEquals(VoiceSessionApi.EntryPoint.NOTIFICATION, sessionEntryPoint("notification_talk"))
        assertTrue(runCatching { sessionEntryPoint("wake:persian-cat") }.isFailure)
    }

    @Test
    fun `ordinary wake detection leaves real coordinator idle without signing`() = runBlocking<Unit> {
        val dispatcher = Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "wake-idle-chain-test").apply { isDaemon = true }
        }.asCoroutineDispatcher()
        val scope = CoroutineScope(SupervisorJob() + dispatcher)
        val signCalls = AtomicInteger(0)
        val coordinator = VoiceSessionCoordinator(
            scope = scope,
            actorDispatcher = dispatcher,
            signSession = { _, _ ->
                signCalls.incrementAndGet()
                CompletableDeferred<VoiceSessionInfo>().await()
            },
            enterRoom = { _, _ -> Unit },
            exitRoom = { _ -> Unit }
        )
        try {
            val service = VoiceForegroundService()
            setPrivateField(service, "micRecorder", MicRecorder { })
            setPrivateField(service, "coordinator", coordinator)

            invokeTriggerWake(service, "persian-cat")
            delay(100)

            assertEquals(VoiceSessionState.IDLE, coordinator.model.value.state)
            assertEquals("普通 KWS 命中不得进入签发效果", 0, signCalls.get())

            coordinator.start("main")
            withTimeout(3_000) {
                coordinator.model.first { it.state == VoiceSessionState.SIGNING }
            }
            assertEquals("显式 P0 入口仍须正常进入签发", 1, signCalls.get())
        } finally {
            scope.cancel()
            dispatcher.close()
        }
    }

    @Test
    fun `service signs with stored credential and explicit entry point`() {
        val service = File(findSourceRoot(), "voice/VoiceForegroundService.kt").readText()

        assertTrue(service.contains("VoiceConfig.deviceSessionCredential(this)"))
        assertTrue(service.contains("deviceId = sessionCredential.deviceId"))
        assertTrue(service.contains("credential = sessionCredential.wireCredential"))
        assertTrue(service.contains("entryPoint = sessionEntryPoint(source)"))
        assertFalse(service.contains("VoiceSessionApi().fetchSession(\n                    baseUrl = VoiceConfig.sessionBaseUrl(this),\n                    deviceId = VoiceConfig.deviceId(this)\n"))
    }

    private fun setPrivateField(target: Any, name: String, value: Any) {
        target.javaClass.getDeclaredField(name).apply {
            isAccessible = true
            set(target, value)
        }
    }

    private fun invokeTriggerWake(service: VoiceForegroundService, keyword: String) {
        service.javaClass.getDeclaredMethod("triggerWake", String::class.java).apply {
            isAccessible = true
            invoke(service, keyword)
        }
    }

    private fun findSourceRoot(): String {
        var dir: File? = File(System.getProperty("user.dir"))
        repeat(4) {
            val candidate = dir?.resolve("src/main/java/com/jax/voice")
            if (candidate != null && candidate.isDirectory) return candidate.absolutePath
            dir = dir?.parentFile
        }
        error("source root not found")
    }
}
