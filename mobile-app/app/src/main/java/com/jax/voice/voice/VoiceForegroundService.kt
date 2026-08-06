package com.jax.voice.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.jax.voice.MainActivity
import com.jax.voice.R
import com.jax.voice.config.VoiceConfig
import com.jax.voice.net.RtcClient
import com.jax.voice.net.VoiceSessionApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 前台服务（"一直在听"核心，spec §4.1）：常驻通知 + AudioRecord 16k PCM16 两路分发
 * （KWS 唤醒 / RMS）。v0.6.0 TRTC 重构（ADR-012 / MOBILE-INTEGRATION §3.2）：
 *
 * 【监听阶段】(一直) MicRecorder ──► FrameDispatcher ──► WakeWordEngine(KWS) / RMS
 * 【唤醒命中】→ 停 MicRecorder（mic handoff，释放 mic）→ REST POST /api/v1/voice/session
 *             拉 roomId+userSig → RtcClient.enterRoom（TRTC SDK 独占采集/播放）
 * 【通话结束】(轻触退房 / onExitRoom 回调) → 重启 MicRecorder 恢复"一直在听"
 *
 * 平台约束（spec §11-1）：Android 14 禁后台启动 mic 前台服务，仅 Activity/通知/悬浮窗启动。
 * 断线重连由 TRTC SDK 内置（无限重连），应用层只映射六态（onConnectionLost/Recovery）。
 */
class VoiceForegroundService : Service() {

    companion object {
        private const val TAG = "VoiceService"
        const val ACTION_START = "com.jax.voice.action.START"
        const val ACTION_STOP = "com.jax.voice.action.STOP"
        const val ACTION_TALK = "com.jax.voice.action.TALK" // 立即对话（悬浮窗/通知兜底，§5.3）
        const val ACTION_PAUSE = "com.jax.voice.action.PAUSE" // 暂停/恢复监听
        const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "voice_listening"
        private const val IDLE_TIMEOUT_MS = 15_000L // spec §4.6 / V-5
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var micRecorder: MicRecorder? = null
    private var wakeEngine: WakeWordEngine? = null
    private var dispatcher: FrameDispatcher? = null
    private var rtcClient: RtcClient? = null
    private var idleTimeoutJob: Job? = null

    /** 会话签发请求任务（进房前取消，防竞态） */
    private var sessionJob: Job? = null

    /** 是否在 RTC 通话中（TRTC mic 独占；true 时再次轻触 = 退房） */
    @Volatile
    private var inCall = false

    /** 唤醒词开关（v0.4.4 默认关：sherpa JNI 原生崩溃嫌疑源，关闭=完全不加载引擎；悬浮球/通知按钮仍可对话） */
    @Volatile
    private var wakeActive = VoiceConfig.WAKE_DEFAULT_ENABLED

    /** mic 管线意外死亡重建计数（防重启风暴） */
    @Volatile
    private var micRestartCount = 0

    /** 服务正在停止：禁止自动重建 */
    @Volatile
    private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_TALK -> {
                // 悬浮窗/通知兜底入口（§5.3）：未启动则先启动管线，再触发唤醒
                if (micRecorder == null) startPipeline()
                triggerWake("interact")
            }

            ACTION_PAUSE -> {
                if (micRecorder == null) startPipeline()
                wakeActive = !wakeActive
                dispatcher?.wakeEnabled = wakeActive
                updateNotificationTitle()
            }

            else -> startPipeline()
        }
        return START_STICKY
    }

    private fun startPipeline() {
        if (micRecorder != null) return // 已在运行
        try {
            startPipelineInner()
        } catch (t: Throwable) {
            // 防御：任何初始化异常不闪退——记日志 + 停止服务 + 通知 UI
            Log.e(TAG, "startPipeline crashed: ${t.message}", t)
            VoiceController.setService(ServiceState.STOPPED)
            stopSelf()
        }
    }

    private fun startPipelineInner() {
        startForegroundCompat()

        // TRTC 通话客户端（替代 VoiceWsClient）：六态/音量/错误/退房回调全部接 VoiceController
        rtcClient = RtcClient(
            appContext = applicationContext,
            onState = { VoiceController.setConnection(it) },
            onPhase = { VoiceController.setPhase(it) },
            onRms = { VoiceController.setRms(it) },
            onError = { code, msg ->
                VoiceController.setLastError("进房失败: $code $msg")
                onCallExited()
            },
            onExited = { onCallExited() }
        )

        // v0.4.4：唤醒词检测按配置（默认关）。关闭时**完全不构造 WakeWordEngine**（零 sherpa JNI 接触，
        // 消除原生崩溃嫌疑）；对话由悬浮球轻触 / 通知按钮 ACTION_TALK 触发（wake 帧照发）
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
            Log.i(TAG, "wake word disabled (v0.4.4 default) — use overlay tap / notification talk")
            null
        }
        wakeEngine = engine

        dispatcher = FrameDispatcher(
            wakeEngine = engine,
            onRms = { rms -> VoiceController.setRms(rms) }
        ).also { it.wakeEnabled = wakeActive }

        micRecorder = MicRecorder { samples -> dispatcher?.onFrame(samples) }
        // mic 逐帧防御 + 假死看门狗：意外死亡 → 上报重建（见 onMicDied）
        micRecorder!!.setOnDied { onMicDied() }
        val ok = micRecorder!!.start()
        if (!ok) {
            Log.e(TAG, "mic start failed")
            stopSelf()
            return
        }
        micRestartCount = 0

        VoiceController.setService(ServiceState.RUNNING)
        VoiceController.setPhase(VoicePhase.MONITORING)
        updateNotificationTitle()
        Log.i(TAG, "pipeline started: mic 16k + KWS + TRTC rtcClient")
    }

    /**
     * mic 管线意外死亡（连续帧失败 / 假死看门狗 / 采集 Error）：
     * 清理半死对象 → 释放 KWS（走其专用线程，安全）→ 上限 3 次自动重建；超限 stopSelf（防风暴）。
     */
    private fun onMicDied() {
        Log.e(TAG, "mic pipeline died (restartCount=$micRestartCount)")
        try {
            idleTimeoutJob?.cancel()
            idleTimeoutJob = null
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

    /**
     * 唤醒（KWS 命中 或 交互兜底触发，spec §5.3）→ mic handoff 进房。
     *
     * - 监听阶段：MicRecorder 常驻采集喂 KWS；唤醒命中 → 停 MicRecorder 释放 mic → TRTC SDK 独占采集上行。
     * - 会话中再触发（轻触/命令词）：退房（MVP 交互式退房，"退下"命令词见 [handleCommandWord] 预留）。
     */
    private fun triggerWake(keyword: String) {
        if (micRecorder == null) return
        if (inCall) {
            // 会话中：再轻触 = 退房（v0.6.0 全双工会话，用户手动停止）
            endCall()
            return
        }
        if (handleCommandWord(keyword)) return
        VoiceController.onWake(keyword)
        startCall()
    }

    /**
     * 命令词处理（Phase B 预留）：服务端文本回调/意图下行暂未接入 TRTC 链路。
     * MVP 方案：说"退下"退房 = 再次轻触悬浮球（本方法为命令词识别预留接口，Phase B 接入后
     * 由 KWS 扩展词/服务端 STT 回调驱动 endCall()）。
     */
    private fun handleCommandWord(word: String): Boolean {
        if (word == "退下") {
            endCall()
            return true
        }
        return false
    }

    /** 唤醒命中 → 停 MicRecorder（mic handoff）→ 拉会话凭证 → RtcClient.enterRoom */
    private fun startCall() {
        if (inCall) return
        val baseUrl = VoiceConfig.sessionBaseUrl(this)
        if (baseUrl.isBlank()) {
            VoiceController.setLastError("请先在设置页填写会话服务器地址（TRTC userSig 签发）")
            return
        }
        // mic handoff：进房前停 MicRecorder 释放 mic（Android 不允许双 AudioRecord 同时采集）
        micRecorder?.stop()
        micRecorder = null
        dispatcher = null
        inCall = true
        VoiceController.setConnection(ConnectionState.CONNECTING)
        VoiceController.setPhase(VoicePhase.LISTENING)

        sessionJob?.cancel()
        sessionJob = scope.launch {
            try {
                val session = VoiceSessionApi().fetchSession(
                    baseUrl = baseUrl,
                    deviceId = VoiceConfig.deviceId(this@VoiceForegroundService)
                )
                rtcClient?.enterRoom(session)
            } catch (t: Throwable) {
                // 会话签发失败：回落到监听态（重启 MicRecorder），不崩溃
                Log.e(TAG, "session fetch failed: ${t.message}", t)
                VoiceController.setLastError("会话签发失败: ${t.message}")
                inCall = false
                onCallExited()
            }
        }
        updateNotificationTitle()
    }

    /** 结束通话：请求退房（异步，等 onExitRoom → [onCallExited] 重启 MicRecorder） */
    private fun endCall() {
        if (!inCall) return
        sessionJob?.cancel()
        inCall = false
        VoiceController.setPhase(VoicePhase.MONITORING)
        rtcClient?.exitRoom()
        updateNotificationTitle()
    }

    /** 退房完成回调（RtcClient.onExitRoom）→ 重启 MicRecorder 恢复"一直在听"（ADR 要求等回调再重启） */
    private fun onCallExited() {
        if (stopping) return
        inCall = false
        idleTimeoutJob?.cancel()
        VoiceController.setConnection(ConnectionState.DISCONNECTED)
        VoiceController.setPhase(VoicePhase.MONITORING)
        VoiceController.setLastError("")
        restartMicRecorder()
        updateNotificationTitle()
    }

    /** 通话结束后重建监听管线（wakeEngine 常驻不释放，仅重建采集 + 分发）
     *  幂等：TRTC 进房失败会同时触发 onEnterRoom(<0) 与 onError 两条回调（各走一次 onCallExited），
     *  已重启则跳过，避免双 AudioRecord 抢占 mic（Android 同一 App 不允许双路同时采集）。 */
    private fun restartMicRecorder() {
        if (micRecorder != null) return
        try {
            val engine = wakeEngine
            val d = FrameDispatcher(
                wakeEngine = engine,
                onRms = { rms -> VoiceController.setRms(rms) }
            ).also { it.wakeEnabled = wakeActive }
            dispatcher = d
            val recorder = MicRecorder { samples -> d.onFrame(samples) }
            recorder.setOnDied { onMicDied() }
            val ok = recorder.start()
            if (ok) {
                micRecorder = recorder
                micRestartCount = 0
                VoiceController.setService(ServiceState.RUNNING)
                VoiceController.setPhase(VoicePhase.MONITORING)
                Log.i(TAG, "mic restarted after call (listening resumed)")
            } else {
                Log.e(TAG, "mic restart after call failed")
                onMicDied()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "restartMicRecorder failed: ${t.message}", t)
        }
    }

    private fun startForegroundCompat() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, getString(R.string.notif_channel_name), NotificationManager.IMPORTANCE_LOW).apply {
                description = getString(R.string.notif_channel_desc)
            }
        )
        val notification = buildNotification(getString(R.string.notif_title))
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification,
            // microphone 前台服务类型：API 29+（Q）即支持类型参数，统一传 MICROPHONE；
            // API 26-28（minSdk）无类型参数概念，传 0（被忽略）
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            } else {
                0
            }
        )
    }

    private fun buildNotification(title: String): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val talk = PendingIntent.getService(
            this, 1,
            Intent(this, VoiceForegroundService::class.java).setAction(ACTION_TALK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val pause = PendingIntent.getService(
            this, 2,
            Intent(this, VoiceForegroundService::class.java).setAction(ACTION_PAUSE),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val exit = PendingIntent.getService(
            this, 3,
            Intent(this, VoiceForegroundService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(getString(R.string.notif_text))
            .setSmallIcon(R.drawable.ic_stat_mic)
            .setOngoing(true)
            .setContentIntent(openApp)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(0, getString(R.string.notif_action_talk), talk)
            .addAction(0, getString(R.string.notif_action_pause), pause)
            .addAction(0, getString(R.string.notif_action_exit), exit)
            .build()
    }

    private fun updateNotificationTitle() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val title = when (VoiceController.ui.value.phase) {
            VoicePhase.LISTENING -> getString(R.string.phase_listening)
            VoicePhase.SPEAKING -> getString(R.string.phase_speaking)
            VoicePhase.THINKING -> getString(R.string.phase_thinking)
            else -> if (wakeActive) getString(R.string.notif_title) else "监听已暂停"
        }
        nm.notify(NOTIFICATION_ID, buildNotification(title))
    }

    override fun onDestroy() {
        stopping = true
        idleTimeoutJob?.cancel()
        sessionJob?.cancel()
        micRecorder?.stop()
        micRecorder = null
        wakeEngine?.release()
        wakeEngine = null
        dispatcher = null
        rtcClient?.release()
        rtcClient = null
        VoiceController.setService(ServiceState.STOPPED)
        VoiceController.setPhase(VoicePhase.IDLE)
        super.onDestroy()
    }
}
