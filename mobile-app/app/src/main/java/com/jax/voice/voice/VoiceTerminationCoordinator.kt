package com.jax.voice.voice

import android.util.Log
import com.jax.voice.config.VoiceConfig
import com.jax.voice.net.RtcClient
import com.jax.voice.net.VoiceSessionApi
import kotlinx.coroutines.CancellationException
import java.time.Instant
import java.util.UUID

internal suspend fun runTerminationNotice(
    service: VoiceForegroundService,
    client: RtcClient,
    terminationApi: VoiceSessionApi,
    signedSession: () -> VoiceSessionInfo?,
    generation: Long,
    retryDelayMs: Long,
    maxRetries: Int,
) {
    try {
        val sent = ExitTerminationFlow.runBeforeExit(
            inRoom = client.isInRoom(),
            signedSession = signedSession(),
            postTerminate = {
                val session = checkNotNull(signedSession())
                val cred = VoiceConfig.deviceSessionCredential(service)
                terminationApi.postTerminate(
                    baseUrl = VoiceConfig.sessionBaseUrl(service),
                    credential = cred.wireCredential,
                    request = VoiceSessionApi.TerminateRequest(
                        sessionId = checkNotNull(session.sessionId), deviceId = cred.deviceId,
                        roomId = session.roomId, generation = generation,
                        requestId = UUID.randomUUID().toString(),
                        reason = VoiceSessionApi.TerminateReason.USER_STOP,
                        requestedAt = Instant.now().toString()
                    )
                )
            },
            sendNotice = { client.sendTerminationNotice(it) },
            retryDelayMs = retryDelayMs, maxRetries = maxRetries
        )
        Log.i("VoiceService", "termination notice sequence done sent=$sent")
    } catch (t: Throwable) {
        if (t is CancellationException) throw t
        Log.w("VoiceService", "termination notice failed (proceeding to exitRoom): ${t.message}")
    }
}
