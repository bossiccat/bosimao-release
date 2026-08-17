package com.jax.voice.net

import android.content.Context
import android.os.Bundle
import android.util.Log
import com.jax.voice.util.DiagLog
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoicePhase
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudDef
import com.tencent.trtc.TRTCCloudListener

/** TRTC audio session client with generation-bound callbacks and idempotent teardown. */
class RtcClient(
    private val appContext: Context,
    private val onState: (ConnectionState) -> Unit,
    private val onPhase: (VoicePhase) -> Unit,
    private val onRms: (Float) -> Unit,
    private val onError: (code: String, msg: String) -> Unit,
    private val onEntered: (generation: Long) -> Unit = {},
    private val onSessionFailure: (generation: Long, code: String, msg: String) -> Unit = { _, _, _ -> },
    private val onExited: () -> Unit,
    private val onRemoteAudioEvent: (RtcPlaybackSubscription.RemoteAudioEvent) -> Unit = {},
    private val engineFactory: (Context) -> TRTCCloud = { ctx -> TRTCCloud.sharedInstance(ctx) },
    private val destroyEngine: () -> Unit = { TRTCCloud.destroySharedInstance() },
    /** Test-only lifecycle observation; invoked after released=true and before SDK lock acquisition. */
    private val onReleaseClaimed: () -> Unit = {},
    private val delayScheduler: (Long, () -> Unit) -> DelayHandle = { ms, callback ->
        val thread = Thread {
            try {
                Thread.sleep(ms)
                callback()
            } catch (_: InterruptedException) {
                // The owning attempt completed before this fallback.
            }
        }.apply { isDaemon = true; start() }
        DelayHandle { thread.interrupt() }
    }
) {
    fun interface DelayHandle {
        fun cancel()
    }

    companion object {
        private const val TAG = "RtcClient"
        private const val VOLUME_INTERVAL_MS = 300
        private const val EXIT_TIMEOUT_MS = 3_000L
        private const val ENTER_TIMEOUT_MS = 15_000L
        private const val REMOTE_LEAVE_TIMEOUT_MS = 60_000L
    }

    private enum class EnterOutcome { PENDING, ENTERED, FAILED, TIMED_OUT, CANCELLED }

    private data class EnterAttempt(
        val generation: Long,
        val listener: TRTCCloudListener,
        var outcome: EnterOutcome = EnterOutcome.PENDING,
        var inRoom: Boolean = false,
        var listenerAttached: Boolean = false,
        var enterIssued: Boolean = false,
        var localAudioStarted: Boolean = false,
        var frameListenerAttached: Boolean = false,
        var exitRequested: Boolean = false,
        var exitIssued: Boolean = false,
        var resourcesReleased: Boolean = false,
        var listenerRemoved: Boolean = false,
        var completionDelivered: Boolean = false,
        var enterTimeout: DelayHandle? = null,
        var exitTimeout: DelayHandle? = null,
        var leaveTimeout: DelayHandle? = null
    )

    private val lifecycleLock = Any()
    /** Serializes the full SDK ownership window against release/destroy. */
    private val sdkOperationLock = Any()
    @Volatile private var attempt: EnterAttempt? = null
    @Volatile private var released = false
    private var cloud: TRTCCloud? = null
    @Volatile private var lastVolLogTs = 0L
    @Volatile private var remoteUserId: String? = null

    private val audioRms = RtcAudioFrameRms(onRms = onRms)
    private val playback = RtcPlaybackSubscription(
        cloud = { requireActiveCloud() },
        onPhase = onPhase,
        onUiEvent = onRemoteAudioEvent
    )

    val playbackGeneration: Int get() = playback.playbackGeneration

    fun enterRoom(session: VoiceSessionApi.VoiceSession, generation: Long = 0L) {
        val current = synchronized(lifecycleLock) {
            if (released) {
                Log.w(TAG, "enterRoom ignored: client already released")
                return
            }
            if (attempt != null) {
                Log.w(TAG, "enterRoom ignored: attempt already owns RTC resources")
                return
            }
            newAttempt(generation).also { attempt = it }
        }

        try {
            synchronized(sdkOperationLock) {
                if (!isCurrentAttempt(current) || isReleased()) return
                val engine = getOrCreateCloud()
                engine.addListener(current.listener)
            current.listenerAttached = true
            val stillOwned = synchronized(lifecycleLock) {
                attempt === current && !current.exitRequested && !released
            }
            if (!stillOwned) return

            val params = TRTCCloudDef.TRTCParams().apply {
                sdkAppId = session.sdkAppId
                userId = session.userId
                userSig = session.userSig
                roomId = 0
                strRoomId = session.roomId
            }
            onPhase(VoicePhase.LISTENING)
            onState(ConnectionState.CONNECTING)
            current.enterIssued = true
            engine.enterRoom(params, TRTCCloudDef.TRTC_APP_SCENE_AUDIOCALL)
            val stillEntering = synchronized(lifecycleLock) {
                attempt === current &&
                    !released &&
                    !current.exitRequested &&
                    current.outcome != EnterOutcome.FAILED &&
                    current.outcome != EnterOutcome.TIMED_OUT &&
                    current.outcome != EnterOutcome.CANCELLED
            }
            if (!stillEntering) return

            current.localAudioStarted = true
            engine.startLocalAudio(TRTCCloudDef.TRTC_AUDIO_QUALITY_SPEECH)
            engine.enableAudioVolumeEvaluation(
                true,
                TRTCCloudDef.TRTCAudioVolumeEvaluateParams().apply {
                    interval = VOLUME_INTERVAL_MS
                    enableVadDetection = false
                }
            )
            engine.setAudioRoute(TRTCCloudDef.TRTC_AUDIO_ROUTE_SPEAKER)
            current.frameListenerAttached = true
            engine.setAudioFrameListener(audioRms.listener())
            try {
                engine.muteAllRemoteAudio(false)
            } catch (t: Throwable) {
                Log.w(TAG, "muteAllRemoteAudio(false) failed: ${t.message}", t)
            }
            scheduleEnterTimeout(current)
                DiagLog.log("Rtc", "enterRoom generation=$generation room=${session.roomId} userId=${session.userId}")
            }
        } catch (t: Throwable) {
            rollbackAttempt(current)
            val message = t.message ?: "RTC engine initialization failed"
            Log.e(TAG, "enterRoom setup failed: $message", t)
            onState(ConnectionState.DISCONNECTED)
            onError("engine_init", message)
            onSessionFailure(generation, "engine_init", message)
        }
    }

    fun exitRoom() {
        val current = synchronized(lifecycleLock) { attempt } ?: return
        if (beginTeardown(current)) scheduleExitTimeout(current)
    }

    fun hasActiveAttempt(): Boolean = synchronized(lifecycleLock) { attempt != null }

    @Deprecated("Use hasActiveAttempt; pending is an attempt outcome, not a timer")
    fun hasPendingEnter(): Boolean = synchronized(lifecycleLock) {
        attempt?.outcome == EnterOutcome.PENDING
    }

    fun interruptRemotePlayback() {
        if (isReleased()) return
        val userId = remoteUserId ?: return
        playback.interruptPlayback(userId)
    }

    fun muteLocal(muted: Boolean) {
        if (isReleased()) return
        synchronized(lifecycleLock) { cloud }?.muteLocalAudio(muted)
    }

    fun isInRoom(): Boolean = synchronized(lifecycleLock) { attempt?.inRoom == true }

    fun release() {
        val current: EnterAttempt?
        synchronized(lifecycleLock) {
            if (released) return
            released = true
            current = attempt
        }
        onReleaseClaimed()

        synchronized(sdkOperationLock) {
            val engine = synchronized(lifecycleLock) { cloud }
            current?.let {
                beginTeardown(it)
                completeTeardown(it, notifyExited = false)
            }
            if (engine != null) {
                synchronized(lifecycleLock) { cloud = null }
                try {
                    destroyEngine()
                } catch (t: Throwable) {
                    Log.e(TAG, "destroy engine failed: ${t.message}", t)
                }
            }
        }
    }

    private fun newAttempt(generation: Long): EnterAttempt {
        lateinit var current: EnterAttempt
        val listener = createAttemptListener { current }
        current = EnterAttempt(generation = generation, listener = listener)
        return current
    }

    private fun createAttemptListener(currentAttempt: () -> EnterAttempt): TRTCCloudListener =
        object : TRTCCloudListener() {
            private fun currentOrNull(): EnterAttempt? = currentAttempt().takeIf(::isCurrentAttempt)
            private fun activeOrNull(): EnterAttempt? = currentOrNull()?.takeUnless { it.exitRequested }

            override fun onEnterRoom(result: Long) {
                val current = activeOrNull() ?: return
                val outcome = if (result >= 0) EnterOutcome.ENTERED else EnterOutcome.FAILED
                if (!claimEnterOutcome(current, outcome)) return
                cancelEnterTimeout(current)
                DiagLog.log("Rtc", "onEnterRoom generation=${current.generation} result=$result")
                if (result >= 0) {
                    synchronized(lifecycleLock) { current.inRoom = true }
                    VoiceController.setLastError("")
                    onState(ConnectionState.CONNECTED)
                    onEntered(current.generation)
                } else {
                    val message = "进房失败: $result"
                    onState(ConnectionState.DISCONNECTED)
                    onError("enter_room", message)
                    onSessionFailure(current.generation, "enter_room", message)
                    if (beginTeardown(current)) scheduleExitTimeout(current)
                }
            }

            override fun onExitRoom(reason: Int) {
                val current = currentOrNull() ?: return
                Log.i(TAG, "onExitRoom generation=${current.generation} reason=$reason")
                onState(ConnectionState.DISCONNECTED)
                completeTeardown(current, notifyExited = true)
            }

            override fun onRemoteUserEnterRoom(userId: String) {
                val current = activeOrNull() ?: return
                cancelLeaveTimeout(current)
                remoteUserId = userId
                VoiceController.setLastError("")
                onState(ConnectionState.CONNECTED)
                playback.onRemoteUserEnterRoom(userId)
            }

            override fun onRemoteUserLeaveRoom(userId: String, reason: Int) {
                val current = activeOrNull() ?: return
                DiagLog.log("Rtc", "remoteLeave user=$userId reason=$reason")
                VoiceController.setLastError("对端已退出")
                onPhase(VoicePhase.LISTENING)
                scheduleRemoteLeaveTimeout(current)
            }

            override fun onFirstAudioFrame(userId: String) {
                if (activeOrNull() == null) return
                remoteUserId = userId
                playback.onFirstAudioFrame(userId)
            }

            override fun onUserVoiceVolume(
                userVolumes: ArrayList<TRTCCloudDef.TRTCVolumeInfo>,
                totalVolume: Int
            ) {
                if (activeOrNull() == null) return
                onRms(totalVolume / 100f)
                val now = System.currentTimeMillis()
                if (totalVolume > 0 && now - lastVolLogTs > 3_000) {
                    lastVolLogTs = now
                    DiagLog.log("Rtc", "voiceVolume total=$totalVolume")
                }
            }

            override fun onConnectionLost() {
                if (activeOrNull() == null) return
                VoiceController.setLastError("网络中断，重连中…")
                onState(ConnectionState.DISCONNECTED)
                onState(ConnectionState.CONNECTING)
            }

            override fun onTryToReconnect() {
                if (activeOrNull() == null) return
                onState(ConnectionState.CONNECTING)
            }

            override fun onConnectionRecovery() {
                if (activeOrNull() == null) return
                VoiceController.setLastError("")
                onState(ConnectionState.CONNECTED)
            }

            override fun onUserAudioAvailable(userId: String, available: Boolean) {
                if (activeOrNull() == null) return
                if (available) {
                    remoteUserId = userId
                    playback.ensureUnmuted(userId)
                }
            }

            override fun onRemoteAudioStatusUpdated(
                userId: String,
                audioStatus: Int,
                reason: Int,
                extraInfo: Bundle?
            ) {
                if (activeOrNull() == null) return
                remoteUserId = userId
                playback.onRemoteAudioStatusUpdated(userId, audioStatus, reason)
            }

            override fun onError(errCode: Int, errMsg: String, extraInfo: Bundle?) {
                val current = activeOrNull() ?: return
                Log.e(TAG, "TRTC error generation=${current.generation}: $errCode $errMsg")
                onError("$errCode", errMsg)
                onSessionFailure(current.generation, "$errCode", errMsg)
                if (current.inRoom) onState(ConnectionState.DISCONNECTED)
            }
        }

    private fun getOrCreateCloud(): TRTCCloud = synchronized(lifecycleLock) {
        check(!released) { "RTC client released" }
        cloud ?: engineFactory(appContext).also { cloud = it }
    }

    private fun requireActiveCloud(): TRTCCloud = synchronized(lifecycleLock) {
        check(!released && attempt != null) { "RTC client has no active attempt" }
        checkNotNull(cloud) { "RTC engine unavailable" }
    }

    private fun isReleased(): Boolean = synchronized(lifecycleLock) { released }

    private fun isCurrentAttempt(current: EnterAttempt): Boolean = synchronized(lifecycleLock) {
        attempt === current
    }

    private fun claimEnterOutcome(current: EnterAttempt, outcome: EnterOutcome): Boolean =
        synchronized(lifecycleLock) {
            if (attempt !== current || current.exitRequested || current.outcome != EnterOutcome.PENDING) {
                false
            } else {
                current.outcome = outcome
                true
            }
        }

    private fun beginTeardown(current: EnterAttempt): Boolean {
        val shouldBegin = synchronized(lifecycleLock) {
            if (attempt !== current || current.exitRequested) false else {
                current.exitRequested = true
                current.inRoom = false
                if (current.outcome == EnterOutcome.PENDING) current.outcome = EnterOutcome.CANCELLED
                true
            }
        }
        if (!shouldBegin) return false

        cancelEnterTimeout(current)
        cancelLeaveTimeout(current)
        onState(ConnectionState.DISCONNECTED)
        releaseAttemptResources(current, removeListener = false)

        val engine = synchronized(lifecycleLock) { cloud }
        val shouldExit = synchronized(lifecycleLock) {
            current.enterIssued && !current.exitIssued && engine != null
        }
        if (!shouldExit) {
            completeTeardown(current, notifyExited = true)
            return false
        }

        synchronized(lifecycleLock) { current.exitIssued = true }
        return try {
            engine!!.exitRoom()
            true
        } catch (t: Throwable) {
            Log.w(TAG, "exitRoom failed: ${t.message}", t)
            completeTeardown(current, notifyExited = true)
            false
        }
    }

    private fun releaseAttemptResources(current: EnterAttempt, removeListener: Boolean = true) {
        val engine = synchronized(lifecycleLock) { cloud }
        val flags = synchronized(lifecycleLock) {
            if (current.resourcesReleased) {
                Triple(false, false, current.listenerAttached)
            } else {
                current.resourcesReleased = true
                Triple(current.localAudioStarted, current.frameListenerAttached, current.listenerAttached)
            }
        }
        if (engine == null) return
        if (flags.first) try {
            engine.stopLocalAudio()
        } catch (t: Throwable) {
            Log.w(TAG, "stop local audio failed: ${t.message}", t)
        }
        if (flags.second) try {
            engine.setAudioFrameListener(null)
        } catch (t: Throwable) {
            Log.w(TAG, "clear audio frame listener failed: ${t.message}", t)
        }
        if (removeListener && flags.third) {
            val shouldRemove = synchronized(lifecycleLock) {
                if (current.listenerRemoved) false else {
                    current.listenerRemoved = true
                    true
                }
            }
            if (shouldRemove) try {
                engine.removeListener(current.listener)
            } catch (t: Throwable) {
                Log.w(TAG, "remove listener failed: ${t.message}", t)
            }
        }
    }

    private fun completeTeardown(current: EnterAttempt, notifyExited: Boolean) {
        cancelEnterTimeout(current)
        cancelExitTimeout(current)
        cancelLeaveTimeout(current)
        releaseAttemptResources(current)
        val shouldNotify = synchronized(lifecycleLock) {
            if (attempt !== current || current.completionDelivered) false else {
                current.completionDelivered = true
                attempt = null
                notifyExited && !released
            }
        }
        if (shouldNotify) onExited()
    }

    private fun rollbackAttempt(current: EnterAttempt) {
        cancelEnterTimeout(current)
        cancelExitTimeout(current)
        cancelLeaveTimeout(current)
        val engine = synchronized(lifecycleLock) { cloud }
        if (current.enterIssued && engine != null && !current.exitIssued) {
            current.exitIssued = true
            try {
                engine.exitRoom()
            } catch (_: Throwable) {
                // The original initialization exception remains the reported cause.
            }
        }
        releaseAttemptResources(current)
        synchronized(lifecycleLock) {
            if (attempt === current) {
                current.completionDelivered = true
                attempt = null
            }
        }
    }

    private fun scheduleEnterTimeout(current: EnterAttempt) {
        val handle = delayScheduler(ENTER_TIMEOUT_MS) {
            if (claimEnterOutcome(current, EnterOutcome.TIMED_OUT)) {
                val message = "进房超时（${ENTER_TIMEOUT_MS / 1000}s 无回调）"
                Log.e(TAG, "onEnterRoom timeout generation=${current.generation}")
                onState(ConnectionState.DISCONNECTED)
                onError("enter_timeout", message)
                onSessionFailure(current.generation, "enter_timeout", message)
                if (beginTeardown(current)) scheduleExitTimeout(current)
            }
        }
        synchronized(lifecycleLock) {
            if (attempt === current && current.outcome == EnterOutcome.PENDING) {
                current.enterTimeout = handle
            } else {
                handle.cancel()
            }
        }
    }

    private fun scheduleExitTimeout(current: EnterAttempt) {
        val handle = delayScheduler(EXIT_TIMEOUT_MS) {
            if (!isCurrentAttempt(current)) return@delayScheduler
            Log.w(TAG, "onExitRoom timeout generation=${current.generation}")
            completeTeardown(current, notifyExited = true)
        }
        synchronized(lifecycleLock) {
            if (attempt === current && current.exitRequested) current.exitTimeout = handle else handle.cancel()
        }
    }

    private fun scheduleRemoteLeaveTimeout(current: EnterAttempt) {
        cancelLeaveTimeout(current)
        val handle = delayScheduler(REMOTE_LEAVE_TIMEOUT_MS) {
            if (isCurrentAttempt(current) && current.inRoom) exitRoom()
        }
        synchronized(lifecycleLock) {
            if (attempt === current && !current.exitRequested) current.leaveTimeout = handle else handle.cancel()
        }
    }

    private fun cancelEnterTimeout(current: EnterAttempt) {
        synchronized(lifecycleLock) { current.enterTimeout.also { current.enterTimeout = null } }?.cancel()
    }

    private fun cancelExitTimeout(current: EnterAttempt) {
        synchronized(lifecycleLock) { current.exitTimeout.also { current.exitTimeout = null } }?.cancel()
    }

    private fun cancelLeaveTimeout(current: EnterAttempt) {
        synchronized(lifecycleLock) { current.leaveTimeout.also { current.leaveTimeout = null } }?.cancel()
    }
}
