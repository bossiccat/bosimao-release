package com.jax.voice.voice

import android.app.Service
import android.content.Intent
import android.media.MediaRecorder
import android.os.IBinder
import android.util.Log
import com.jax.voice.R
import com.jax.voice.config.VoiceConfig
import com.jax.voice.net.RtcClient
import com.jax.voice.net.VoiceSessionApi
import com.jax.voice.util.DeviceEnvObserver
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.util.concurrent.TimeUnit

internal fun sessionEntryPoint(source: String): VoiceSessionApi.EntryPoint = when {
    source == "main" -> VoiceSessionApi.EntryPoint.MAIN
    source == "overlay" -> VoiceSessionApi.EntryPoint.OVERLAY
    source == "notification" || source == "notification_talk" ->
        VoiceSessionApi.EntryPoint.NOTIFICATION
    source.startsWith("wake:") -> VoiceSessionApi.EntryPoint.MAIN
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

        // Task #23 终止通知预算：HTTP callTimeout 封顶 + 2×500ms RTC 通知重试 ≈ ≤1.7s（< ~2s，
        // 保证 App 被杀场景下退出主路径不被阻塞；CP 超时兜底已有）
        private const val TERMINATE_HTTP_BUDGET_MS = 600L
        private const val NOTICE_RETRY_DELAY_MS = 500L
        private const val NOTICE_RETRY_MAX = 2
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var micRecorder: MicRecorder? = null
    private var wakeEngine: WakeWordEngine? = null
    private var dispatcher: FrameDispatcher? = null
    private var rtcClient: RtcClient? = null
    private var bargeInController: BargeInController? = null
    private var coordinator: VoiceSessionCoordinator? = null
    private var notifications: VoiceServiceNotifications? = null
    private var exitGate: CompletableDeferred<Unit>? = null
    private var enterGate: CompletableDeferred<Unit>? = null
    private var deviceEnvObserver: DeviceEnvObserver? = null

    @Volatile private var wakeActive = VoiceConfig.WAKE_DEFAULT_ENABLED
    @Volatile private var micRestartCount = 0
    @Volatile private var stopping = false

    /**
     * 管线存活判据：必须与 micRecorder 解耦。
     *
     * micRecorder 会被 [stopMicForCall] 置 null（会话期把 mic 让给 RealCustomAudioSource），
     * 用它当判据等于「判据恒为空」——真机实测 4 次点击 = 4 个 coordinator + 5 条
     * jax-rtc-capture 线程。0 = 未构建；>0 = 已构建，值为构建序号，回调用它做代际校验。
     */
    @Volatile private var pipelineSeq = 0

    /** 构建中标记：阻断 releasePipeline 期间旧 coordinator 异步回 IDLE 触发的 restartMicRecorder 抢建 */
    @Volatile private var buildingPipeline = false

    /** 最近一次签发的会话信息（Task #23：退出时提供 terminate 所需 room_id/sessionId 上下文） */
    @Volatile private var lastSignedSession: VoiceSessionInfo? = null

    /** 终止 HTTP 客户端：callTimeout 封顶，保证退出主路径预算（默认 client 是 10s 超时） */
    private val terminationApi by lazy {
        VoiceSessionApi(
            client = okhttp3.OkHttpClient.Builder()
                .callTimeout(TERMINATE_HTTP_BUDGET_MS, TimeUnit.MILLISECONDS)
                .build()
        )
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // 取证可观测性：场景4/场景5 设备环境观测（纯观测，零行为变更）。
        // 与 onDestroy 对称注册/反注册，避免泄漏。
        deviceEnvObserver = DeviceEnvObserver(applicationContext).also { it.start() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_TALK -> {
                // 播放中点击同样是显式打断；非 SPEAKING 时由控制器幂等忽略。
                bargeInController?.interrupt("tap")
                // P0 独立入口（悬浮窗/通知，§5.3）：保证管线后投递同一 Start 命令
                // 不再判 micRecorder —— 它在会话期恒为 null，判了必然每次点击都重建管线
                startPipeline()
                // Task 8：三入口统一命令，source 来自 Intent（main/overlay/notification）
                coordinator?.start(VoiceEntry.resolveSource(intent, "notification_talk"))
            }
            ACTION_PAUSE -> {
                startPipeline()
                wakeActive = !wakeActive
                dispatcher?.wakeEnabled = wakeActive
                updateNotificationTitle()
            }            else -> startPipeline()
        }
        return START_STICKY
    }

    private fun startPipeline() {
        // 判据只看 pipelineSeq：micRecorder 在会话期恒为 null，用它判断会每次点击都重建整条管线
        if (pipelineSeq > 0) {
            Log.d(TAG, "startPipeline skipped: already built seq=$pipelineSeq")
            return
        }
        try {
            startPipelineInner()
        } catch (t: Throwable) {
            Log.e(TAG, "startPipeline crashed: ${t.message}", t)
            VoiceController.setService(ServiceState.STOPPED)
            stopSelf()
        }
    }

    /**
     * 释放整条管线（构建前/服务销毁时调用）。顺序：先 cancel 会话 → 再 release 采集与 RTC →
     * 最后 release 唤醒引擎。每步独立 try/catch，避免一处抛错导致后面全漏。
     *
     * 关于 [RtcClient.release] 内部的 `TRTCCloud.destroySharedInstance()`：
     * TRTCCloud 是进程级单例，destroy 之后再次 `sharedInstance(ctx)` 会重建全新实例
     * （SDK 官方生命周期即如此设计），而新 RtcClient 的 cloud 是 `by lazy`，取用时才创建，
     * 因此「先 destroy 旧的、再 lazy 建新的」顺序是安全的。不 release 反而更糟——旧 client
     * 的 listener 会永久挂在共享 TRTCCloud 上，回调扇出到已废弃的管线。
     */
    private fun releasePipeline() {
        Log.i(TAG, "pipeline released seq=$pipelineSeq")
        try { coordinator?.cancel() } catch (t: Throwable) { Log.w(TAG, "coordinator cancel failed: ${t.message}") }
        try { rtcClient?.release() } catch (t: Throwable) { Log.w(TAG, "rtcClient release failed: ${t.message}") }
        try { wakeEngine?.release() } catch (t: Throwable) { Log.w(TAG, "wakeEngine release failed: ${t.message}") }
        coordinator = null
        rtcClient = null
        wakeEngine = null
        bargeInController = null
    }

    private fun startPipelineInner() {
        buildingPipeline = true
        releasePipeline()
        pipelineSeq++
        Log.i(TAG, "pipeline built seq=$pipelineSeq")
        // 代际戳：本条管线创建的所有回调用它校验自己是否仍属于「当前」管线
        val seq = pipelineSeq
        /**
         * 陈旧回调守卫：旧管线的 RtcClient 回调不得再打到当前 coordinator / gate 上。
         * 真机故障「16:57:47 才进房的会话被 16:57:49 的 enter_timeout 踢掉」即由此串台造成。
         * 注意只用于低频回调；onRms/onState/onPhase 走静默判等，避免日志风暴。
         */
        fun stale(what: String): Boolean {
            if (pipelineSeq == seq) return false
            Log.w(TAG, "stale callback dropped: $what seq=$seq current=$pipelineSeq")
            return true
        }
        notifications = VoiceServiceNotifications(this)
        notifications!!.startForegroundCompat()
        rtcClient = RtcClient(
            appContext = applicationContext,
            onState = { if (pipelineSeq == seq) VoiceController.setConnection(it) },
            onPhase = {
                if (pipelineSeq == seq) {
                    VoiceController.setPhase(it)
                    val experience = ExperienceState.fromPhase(it)
                    bargeInController?.onExperienceChange(experience)
                    VoiceController.publishExperience(experience)
                }
            },
            onRms = { if (pipelineSeq == seq) VoiceController.setRms(it) },
            onLocalVoiceActivity = { if (pipelineSeq == seq) bargeInController?.interrupt("user_voice") },
            onError = { code, msg ->
                // 守卫前置：旧管线的 enter_timeout 打到当前 coordinator 会直接踢掉刚进房的会话
                if (!stale("onError($code)")) {
                    if (code == "apm_reconnect_gave_up") {
                        VoiceController.publishError(code, msg)
                    } else {
                        VoiceController.setLastError("进房失败: $code $msg")
                    }
                    coordinator?.postFailure(code, msg)
                }
            },
            onExited = { if (!stale("onExited")) exitGate?.complete(Unit) },
            onEntered = { if (!stale("onEntered")) enterGate?.complete(Unit) }
        )
        bargeInController = BargeInController(
            interruptPlayback = { rtcClient?.interruptRemotePlayback() },
            onExperience = { VoiceController.publishExperience(it) }
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
        // 防御：releasePipeline 期间旧 coordinator 若异步回 IDLE 并抢建了 MicRecorder，
        // 直接覆盖会漏掉它的 AudioRecord 线程（正是本次要根治的泄漏类型）
        micRecorder?.stop()
        micRecorder = null
        micRecorder = MicRecorder({ samples -> dispatcher?.onFrame(samples) }, captureSourceFromRes())
        micRecorder!!.setOnDied { onMicDied() }
        if (!micRecorder!!.start()) {
            Log.e(TAG, "mic start failed")
            buildingPipeline = false
            stopSelf()
            return
        }
        micRestartCount = 0
        VoiceController.setService(ServiceState.RUNNING)
        VoiceController.setPhase(VoicePhase.MONITORING)
        updateNotificationTitle()
        Log.i(TAG, "pipeline started: mic 16k + KWS + serialized coordinator")
        buildingPipeline = false
    }

    /** M0 A/B：从 gradle resValue 注入的 jax_capture_source 解析采集源（缺省/异常一律回退 MIC=生产行为）。
     *  RealCustomAudioSource 侧经 resolveCaptureSource() 自读同一 resValue（ActivityThread 反射），无需服务接线。 */
    private fun captureSourceFromRes(): Int {
        return try {
            val resId = resources.getIdentifier("jax_capture_source", "string", packageName)
            val name = if (resId != 0) getString(resId) else "MIC"
            Log.i(TAG, "jax_capture_source=$name")
            if (name == "VOICE_COMMUNICATION") MediaRecorder.AudioSource.VOICE_COMMUNICATION
            else MediaRecorder.AudioSource.MIC
        } catch (t: Throwable) {
            Log.w(TAG, "captureSourceFromRes fallback MIC: ${t.message}")
            MediaRecorder.AudioSource.MIC
        }
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
                VoiceSessionInfo(
                    roomId = s.roomId,
                    userId = s.userId,
                    userSig = s.userSig,
                    sdkAppId = s.sdkAppId,
                    sessionId = s.sessionId,
                    expiresAtEpochMs = s.expiresAtEpochMs
                ).also {
                    lastSignedSession = it // Task #23：退出时 terminate 上下文来源
                }
            },
            enterRoom = { gen, session ->
                val client = rtcClient ?: throw IllegalStateException("rtc client not ready")
                val gate = CompletableDeferred<Unit>()
                enterGate = gate
                try {
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
                    gate.await() // 等真实 onEnterRoom(result>=0) 回调（RtcClient 15s 兜底）；失败经 onError→postFailure 收敛
                } finally {
                    enterGate = null
                }
                coordinator?.postEnterSucceeded(gen)
            },
            exitRoom = { gen ->
                val client = rtcClient
                if (client != null && (client.isInRoom() || client.hasPendingEnter())) {
                    // Task #23：退房前上游接线（postTerminate → RTC 终止通知，含短重试）；
                    // 任何失败照常退房（CP 超时兜底已有），总预算 ≤~2s
                    runTerminationNoticeBeforeExit(client, gen)
                    val gate = CompletableDeferred<Unit>()
                    exitGate = gate
                    client.exitRoom()
                    gate.await() // 等真实退房回调（RtcClient 3s 兜底）；coordinator 退出超时再兜底
                }
            },
            onModel = { renderModel(it) }
        )
    }

    /**
     * 退出前上游接线（Task #23）：IN_ROOM 且有 sessionId 时 postTerminate 拿 tid →
     * sendTerminationNotice（false 时 2 次×500ms 短重试）→ 返回后调用方 exitRoom。
     * fail-open：任何异常吞掉照常退房；CancellationException 原样上抛不拦截。
     */
    private suspend fun runTerminationNoticeBeforeExit(client: RtcClient, generation: Long) =
        runTerminationNotice(
            service = this,
            client = client,
            terminationApi = terminationApi,
            signedSession = { lastSignedSession },
            generation = generation,
            retryDelayMs = NOTICE_RETRY_DELAY_MS,
            maxRetries = NOTICE_RETRY_MAX,
        )

    /** 只渲染模型：mic handoff + 发布统一体验状态 + 兼容存量 VoiceController + 通知 */
    private fun renderModel(model: VoiceSessionModel) {
        when (model.state) {
            VoiceSessionState.IDLE -> {
                lastSignedSession = null // 会话已收敛：清 terminate 上下文（Task #23）
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
    /** KWS 命中后直接进入真实会话；命令词仍优先处理，不得误触发签发。 */
    private fun triggerWake(keyword: String) {
        if (micRecorder == null) return
        if (handleCommandWord(keyword)) return
        VoiceController.onWake(keyword)
        coordinator?.start("wake:$keyword")
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
            // 必须归零：否则 startPipeline() 的 pipelineSeq 守卫会永久拦住重建（P0-1 唯一回归点）
            pipelineSeq = 0
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
        // 管线构建中：旧 coordinator 回 IDLE 触发的重建必须让位，否则会被随后创建的 MicRecorder 覆盖成泄漏
        if (buildingPipeline) {
            Log.d(TAG, "restartMicRecorder skipped: pipeline building")
            return
        }
        try {
            val engine = wakeEngine
            val d = FrameDispatcher(wakeEngine = engine, onRms = { VoiceController.setRms(it) })
                .also { it.wakeEnabled = wakeActive }
            dispatcher = d
            val recorder = MicRecorder({ samples -> d.onFrame(samples) }, captureSourceFromRes())
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
        // 对称反注册：避免 AudioDeviceCallback / 广播 / NetworkCallback 泄漏
        deviceEnvObserver?.stop()
        deviceEnvObserver = null
        micRecorder?.stop()
        micRecorder = null
        dispatcher = null
        releasePipeline() // coordinator.cancel() + rtcClient.release() + wakeEngine.release() + 全部置 null
        pipelineSeq = 0   // 服务销毁：seq 归零，允许下次 onCreate 重新构建
        scope.cancel()
        VoiceController.setService(ServiceState.STOPPED)
        VoiceController.setPhase(VoicePhase.IDLE)
        super.onDestroy()
    }
}
