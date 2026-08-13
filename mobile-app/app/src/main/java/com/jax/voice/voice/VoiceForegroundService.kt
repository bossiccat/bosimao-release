package com.jax.voice.voice

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import com.jax.voice.R
import com.jax.voice.config.VoiceConfig
import com.jax.voice.net.RtcClient
import com.jax.voice.net.VoiceSessionApi
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

internal fun sessionEntryPoint(source: String): VoiceSessionApi.EntryPoint = when {
    source == "main" -> VoiceSessionApi.EntryPoint.MAIN
    source == "overlay" -> VoiceSessionApi.EntryPoint.OVERLAY
    source == "notification" || source == "notification_talk" ->
        VoiceSessionApi.EntryPoint.NOTIFICATION
    source.startsWith("wake:") ->
        throw IllegalStateException("wake word is not a P0 session entry point")
    else -> throw IllegalArgumentException("unsupported P0 voice entry point: $source")
}

/**
 * 前台服务：只发送会话命令并渲染 VoiceSessionModel（SPEC §4.2 / ADR-016）。
 * 不再持有 inCall/rtcExiting 等并行业务布尔量——会话由 VoiceSessionCoordinator 串行唯一裁决：
 * `IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE`，任何结束路径收敛回 IDLE。
 * 常驻监听（MicRecorder -> FrameDispatcher -> KWS/RMS）不承载会话状态；mic handoff 由模型驱动。
 * ACTION_TALK 等常量保留供 Task 8 三入口使用（经 VoiceEntry 统一命令层）；通知通道不删除。
 */
class VoiceForegroundService : Service() {

    companion object {
        private const val TAG = "VoiceService"
        const val ACTION_START = "com.jax.voice.action.START"
        const val ACTION_STOP = "com.jax.voice.action.STOP"
        const val ACTION_TALK = "com.jax.voice.action.TALK" // 立即对话（悬浮窗/通知兜底，§5.3）
        const val ACTION_PAUSE = "com.jax.voice.action.PAUSE" // 暂停/恢复监听
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var micRecorder: MicRecorder? = null
    private var wakeEngine: WakeWordEngine? = null
    private var dispatcher: FrameDispatcher? = null
    private var rtcClient: RtcClient? = null
    private var coordinator: VoiceSessionCoordinator? = null
    private var notifications: VoiceServiceNotifications? = null
    private var exitGate: CompletableDeferred<Unit>? = null

    @Volatile private var wakeActive = VoiceConfig.WAKE_DEFAULT_ENABLED
    @Volatile private var micRestartCount = 0
    @Volatile private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_TALK -> {
                // P0 独立入口（悬浮窗/通知，§5.3）：保证管线后投递同一 Start 命令
                if (micRecorder == null) startPipeline()
                // Task 8：三入口统一命令，source 来自 Intent（main/overlay/notification）
                coordinator?.start(VoiceEntry.resolveSource(intent, "notification_talk"))
            }
            ACTION_PAUSE -> {
                if (micRecorder == null) startPipeline()
                wakeActive = !wakeActive
                dispatcher?.wakeEnabled = wakeActive
                updateNotificationTitle()
            }            else -> startPipeline()
        }
        return START_STICKY
    }

    private fun startPipeline() {
        if (micRecorder != null) return
        try {
            startPipelineInner()
        } catch (t: Throwable) {
            Log.e(TAG, "startPipeline crashed: ${t.message}", t)
            VoiceController.setService(ServiceState.STOPPED)
            stopSelf()
        }
    }

    private fun startPipelineInner() {
        notifications = VoiceServiceNotifications(this)
        notifications!!.startForegroundCompat()
        rtcClient = RtcClient(
            appContext = applicationContext,
            onState = { VoiceController.setConnection(it) },
            onPhase = {
                VoiceController.setPhase(it)
                VoiceController.publishExperience(ExperienceState.fromPhase(it))
            },
            onRms = { VoiceController.setRms(it) },
            onError = { code, msg ->
                VoiceController.setLastError("进房失败: $code $msg")
                coordinator?.postFailure(code, msg)
            },
            onExited = { exitGate?.complete(Unit) }
        )
        coordinator = buildCoordinator()

        wakeActive = VoiceConfig.wakeEnabled(this)
        val engine = if (wakeActive) {
            WakeWordEngine(
                assetManager = assets,
                threshold = VoiceConfig.threshold(this),
                onWake = { keyword -> triggerWake(keyword) },
                onReady = { ok ->
                    Log.i(TAG, "KWS model ready=$ok")
                    updateNotificationTitle()
                }
            )
        } else {
            Log.i(TAG, "wake word disabled — use overlay tap / notification talk")
            null
        }
        wakeEngine = engine

        dispatcher = FrameDispatcher(wakeEngine = engine, onRms = { VoiceController.setRms(it) })
            .also { it.wakeEnabled = wakeActive }
        micRecorder = MicRecorder { samples -> dispatcher?.onFrame(samples) }
        micRecorder!!.setOnDied { onMicDied() }
        if (!micRecorder!!.start()) {
            Log.e(TAG, "mic start failed")
            stopSelf()
            return
        }
        micRestartCount = 0
        VoiceController.setService(ServiceState.RUNNING)
        VoiceController.setPhase(VoicePhase.MONITORING)
        updateNotificationTitle()
        Log.i(TAG, "pipeline started: mic 16k + KWS + serialized coordinator")
    }

    /** 效果注入：签发/进房/退房只在此接线，会话裁决全部交给 coordinator */
    private fun buildCoordinator(): VoiceSessionCoordinator {
        return VoiceSessionCoordinator(
            scope = scope,
            signSession = { _, source ->
                val sessionCredential = VoiceConfig.deviceSessionCredential(this)
                val s = VoiceSessionApi().fetchSession(
                    baseUrl = VoiceConfig.sessionBaseUrl(this),
                    deviceId = sessionCredential.deviceId,
                    credential = sessionCredential.wireCredential,
                    entryPoint = sessionEntryPoint(source)
                )
                VoiceSessionInfo(s.roomId, s.userId, s.userSig, s.sdkAppId, s.sessionId)
            },
            enterRoom = { _, session ->
                val client = rtcClient ?: throw IllegalStateException("rtc client not ready")
                client.enterRoom(
                    VoiceSessionApi.VoiceSession(
                        roomId = session.roomId,
                        userId = session.userId,
                        userSig = session.userSig,
                        sdkAppId = session.sdkAppId,
                        scene = "trtc_full_duplex",
                        sessionId = session.sessionId
                    )
                )
            },
            exitRoom = { _ ->
                val client = rtcClient
                if (client != null && (client.isInRoom() || client.hasPendingEnter())) {
                    val gate = CompletableDeferred<Unit>()
                    exitGate = gate
                    client.exitRoom()
                    gate.await() // 等真实退房回调（RtcClient 3s 兜底）；coordinator 退出超时再兜底
                }
            },
            onModel = { renderModel(it) }
        )
    }

    /** 只渲染模型：mic handoff + 发布统一体验状态 + 兼容存量 VoiceController + 通知 */
    private fun renderModel(model: VoiceSessionModel) {
        when (model.state) {
            VoiceSessionState.IDLE -> {
                VoiceController.setConnection(ConnectionState.DISCONNECTED)
                VoiceController.setPhase(VoicePhase.MONITORING)
                VoiceController.setLastError(model.error ?: "")
                VoiceController.publishExperience(ExperienceState.fromSession(model.state, model.error != null))
                restartMicRecorder()
            }
            VoiceSessionState.SIGNING, VoiceSessionState.ENTERING -> {
                stopMicForCall()
                VoiceController.setConnection(ConnectionState.CONNECTING)
                VoiceController.setPhase(VoicePhase.LISTENING)
                VoiceController.publishExperience(ExperienceState.CONNECTING)
            }
            VoiceSessionState.IN_ROOM -> {
                stopMicForCall()
                VoiceController.setPhase(VoicePhase.LISTENING) // 细化由 RtcClient onPhase 驱动
            }
            VoiceSessionState.EXITING -> VoiceController.publishExperience(ExperienceState.CONNECTING) // "正在结束会话"
        }
        updateNotificationTitle()
    }

    /** mic handoff：会话期停 MicRecorder 释放 mic（Android 不允许双 AudioRecord 同时采集） */
    private fun stopMicForCall() {
        micRecorder?.stop()
        micRecorder = null
        dispatcher = null
    }
    /** 唤醒词属于 P1 Beta：普通 KWS 命中只更新本地状态，不触发 P0 签发/进房。 */
    private fun triggerWake(keyword: String) {
        if (micRecorder == null) return
        if (handleCommandWord(keyword)) return
        VoiceController.onWake(keyword)
    }

    /** 命令词（Phase B 预留）：说"退下" = 取消当前会话 */
    private fun handleCommandWord(word: String): Boolean {
        if (word == "退下") {
            coordinator?.cancel()
            return true
        }
        return false
    }

    /** mic 管线意外死亡：清理半死对象 → 上限 3 次自动重建；超限 stopSelf（防重启风暴） */
    private fun onMicDied() {
        Log.e(TAG, "mic pipeline died (restartCount=$micRestartCount)")
        try {
            micRecorder = null
            dispatcher = null
            wakeEngine?.release()
            wakeEngine = null
            VoiceController.setService(ServiceState.STOPPED)
            VoiceController.setPhase(VoicePhase.IDLE)
            if (micRestartCount < 3) {
                micRestartCount++
                Log.w(TAG, "rebuilding pipeline (attempt $micRestartCount)")
                scope.launch {
                    delay(300L)
                    if (micRecorder == null && !stopping) startPipeline()
                }
            } else {
                Log.e(TAG, "mic died 3+ times, stop service")
                stopSelf()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "onMicDied failed: ${t.message}", t)
        }
    }

    /** 会话结束后重建监听管线（幂等：双回调只重建一次，防双 AudioRecord 抢占 mic） */
    private fun restartMicRecorder() {
        if (micRecorder != null) return
        try {
            val engine = wakeEngine
            val d = FrameDispatcher(wakeEngine = engine, onRms = { VoiceController.setRms(it) })
                .also { it.wakeEnabled = wakeActive }
            dispatcher = d
            val recorder = MicRecorder { samples -> d.onFrame(samples) }
            recorder.setOnDied { onMicDied() }
            if (recorder.start()) {
                micRecorder = recorder
                micRestartCount = 0
                VoiceController.setService(ServiceState.RUNNING)
                VoiceController.setPhase(VoicePhase.MONITORING)
                Log.i(TAG, "mic restarted after session (listening resumed)")
            } else {
                Log.e(TAG, "mic restart after session failed")
                onMicDied()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "restartMicRecorder failed: ${t.message}", t)
        }
    }
    /** 通知渲染切换到统一 VoiceUiModel（Task 8：不再读旧 VoicePhase 拼装） */
    private fun updateNotificationTitle() {
        val title = when (VoiceController.uiModel.value.experience) {
            ExperienceState.LISTENING -> getString(R.string.phase_listening)
            ExperienceState.SPEAKING -> getString(R.string.phase_speaking)
            ExperienceState.THINKING -> getString(R.string.phase_thinking)
            ExperienceState.CONNECTING, ExperienceState.RECOVERING -> getString(R.string.conn_connecting)
            ExperienceState.ERROR -> getString(R.string.conn_disconnected)
            else -> if (wakeActive) getString(R.string.notif_title) else "监听已暂停"
        }
        notifications?.update(title)
    }

    override fun onDestroy() {
        stopping = true
        coordinator = null
        micRecorder?.stop()
        micRecorder = null
        wakeEngine?.release()
        wakeEngine = null
        dispatcher = null
        rtcClient?.release()
        rtcClient = null
        scope.cancel()
        VoiceController.setService(ServiceState.STOPPED)
        VoiceController.setPhase(VoicePhase.IDLE)
        super.onDestroy()
    }
}
