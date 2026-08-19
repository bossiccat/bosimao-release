package com.jax.voice.net

import android.content.Context
import android.os.Bundle
import android.util.Log
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoicePhase
import com.jax.voice.util.DiagLog
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudDef
import com.tencent.trtc.TRTCCloudListener

/**
 * TRTC 通话客户端 —— 纯音频 1v1 通话；替代 VoiceWsClient（WS+配对）。
 * 依据：docs/rtc-rebuild/MOBILE-INTEGRATION.md §2/§3.4 / ARCHITECTURE.md §5.1 / ADR-012；
 * API 签名对照官方 TRTC Android SDK 13.4.0.20477 实际 jar（javap 核对，非记忆）。
 *
 * 职责边界（Task 7 拆分）：
 * - 会话核心：进房/退房/断线重连映射/错误/对端离开超时退房（本类）。
 * - 远端播放订阅与打断：[RtcPlaybackSubscription]（正常停止只发 UI 事件，绝不 mute，AC-12/13/14）。
 * - 采集波形：[RtcAudioFrameRms]（本地帧 RMS 与 onUserVoiceVolume 双源互补）。
 *
 * 要点：仅会话期进房（常驻监听不耗 RTC 分钟）；mic handoff 由调用方停 MicRecorder、等 onExitRoom
 * （3s 超时兜底）；对端离开 60s 未重进自动退房；断线重连 SDK 内置，应用层只映射连接状态；
 * 播放走 SDK 自动订阅（不注册 onAudioFrame 接管）；DTLS-SRTP 加密，App 不持有 SecretKey。
 */
class RtcClient(
    private val appContext: Context,
    private val onState: (ConnectionState) -> Unit,
    private val onPhase: (VoicePhase) -> Unit,
    private val onRms: (Float) -> Unit,
    private val onError: (code: String, msg: String) -> Unit,
    /** 真实 SDK onEnterRoom(result >= 0) 后触发，不能用 enterRoom() 同步返回替代。 */
    private val onEntered: () -> Unit = {},
    /** 退房完成回调（onExitRoom 触发；超时兜底也会触发）：调用方在此重启 MicRecorder 恢复"一直在听" */
    private val onExited: () -> Unit,
    /** 远端播放 UI 事件（Task 7：正常停止 = RemoteAudioStopped，Task 8 接入 BargeInController） */
    private val onRemoteAudioEvent: (RtcPlaybackSubscription.RemoteAudioEvent) -> Unit = {},
    /** 引擎工厂（测试注入，QA L0 RTC-CLIENT-TEST-DESIGN §2）：默认 TRTCCloud.sharedInstance(appContext) */
    private val engineFactory: (Context) -> TRTCCloud = { ctx -> TRTCCloud.sharedInstance(ctx) }
) {
    companion object {
        private const val TAG = "RtcClient"
        // 常量（MOBILE-INTEGRATION §1.3 / P2-2 / v0.6.2 / P2-3）
        private const val VOLUME_INTERVAL_MS = 300 // 音量回调间隔
        private const val EXIT_TIMEOUT_MS = 3_000L // 退房回调超时兜底
        private const val ENTER_TIMEOUT_MS = 15_000L // 进房回调超时兜底
        private const val REMOTE_LEAVE_TIMEOUT_MS = 60_000L // 对端离开超时退房
    }

    @Volatile private var inRoom = false // 本地维护；13.4 SDK 无 isInRoom 公开方法
    @Volatile private var exitHandled = false // 防「超时兜底 + 真实回调」双触发
    @Volatile private var exitTimeoutThread: Thread? = null
    @Volatile private var enterTimeoutThread: Thread? = null
    @Volatile private var leaveTimeoutThread: Thread? = null
    @Volatile private var lastVolLogTs = 0L // 非零音量降频记录（3s 一条）
    @Volatile private var remoteUserId: String? = null // 最近远端用户（打断 flush 目标）

    private val audioRms = RtcAudioFrameRms(onRms = { onRms(it) }) // 本地采集帧 RMS（波形兜底源）

    /** TRTC 引擎（默认 App 进程级单例 sharedInstance）；懒加载：首次 enterRoom 才创建实例 */
    private val cloud: TRTCCloud by lazy {
        engineFactory(appContext).also { it.addListener(listener) }
    }

    /** 远端播放订阅与打断（Task 7：正常停止只发 UI 事件，显式打断走本地 stop/flush + generation） */
    private val playback = RtcPlaybackSubscription(
        cloud = { cloud },
        onPhase = { onPhase(it) },
        onUiEvent = { onRemoteAudioEvent(it) }
    )

    /** 播放代数（Task 7）：显式打断递增，旧 generation 下行帧失效（AC-14） */
    val playbackGeneration: Int get() = playback.playbackGeneration

    private val listener = object : TRTCCloudListener() {
        override fun onEnterRoom(result: Long) {
            cancelEnterTimeout()
            DiagLog.log("Rtc", "onEnterRoom result=$result")
            // 判成功：真实 SDK result>0=成功（耗时ms）、result<0=失败；result==0 为测试 mock 成功哨兵
            if (result >= 0) {
                inRoom = true
                VoiceController.setLastError("")
                onState(ConnectionState.CONNECTED)
                onEntered()
            } else {
                inRoom = false
                onState(ConnectionState.DISCONNECTED)
                onError("enter_room", "进房失败: $result")
            }
        }
        override fun onExitRoom(reason: Int) {
            Log.i(TAG, "onExitRoom reason=$reason (0主动退出/1被踢/2房间解散)")
            cancelExitTimeout()
            cancelLeaveTimeout()
            inRoom = false
            onState(ConnectionState.DISCONNECTED)
            if (!exitHandled) {
                exitHandled = true
                onExited()
            }
        }
        override fun onRemoteUserEnterRoom(userId: String) {
            cancelLeaveTimeout()
            remoteUserId = userId
            VoiceController.setLastError("")
            onState(ConnectionState.CONNECTED)
            playback.onRemoteUserEnterRoom(userId)
        }
        override fun onRemoteUserLeaveRoom(userId: String, reason: Int) {
            DiagLog.log("Rtc", "remoteLeave user=$userId reason=$reason")
            VoiceController.setLastError("对端已退出")
            onPhase(VoicePhase.LISTENING)
            scheduleRemoteLeaveTimeout()
        }
        override fun onFirstAudioFrame(userId: String) {
            remoteUserId = userId
            playback.onFirstAudioFrame(userId)
        }
        override fun onUserVoiceVolume(userVolumes: ArrayList<TRTCCloudDef.TRTCVolumeInfo>, totalVolume: Int) {
            onRms(totalVolume / 100f) // 0~100 → 0~1 归一化驱动悬浮窗波形（本地+远端合计音量）
            val now = System.currentTimeMillis()
            if (totalVolume > 0 && now - lastVolLogTs > 3000) {
                lastVolLogTs = now
                DiagLog.log("Rtc", "voiceVolume total=$totalVolume")
            }
        }
        override fun onConnectionLost() {
            // 断连（约连续 8s 未连上）→ 先 DISCONNECTED 中间态，再 CONNECTING（QA-PLAN §2 A，P2-4）
            VoiceController.setLastError("网络中断，重连中…")
            onState(ConnectionState.DISCONNECTED)
            onState(ConnectionState.CONNECTING)
        }
        override fun onTryToReconnect() {
            onState(ConnectionState.CONNECTING) // 断连 3s 后开始尝试（之后每 24s 重试）
        }
        override fun onConnectionRecovery() {
            VoiceController.setLastError("")
            onState(ConnectionState.CONNECTED)
        }
        override fun onUserAudioAvailable(userId: String, available: Boolean) {
            DiagLog.log("Rtc", "userAudioAvailable user=$userId available=$available")
            if (available) {
                remoteUserId = userId
                playback.ensureUnmuted(userId) // 兜底：确保订阅未被任何静音状态挡住
            }
        }
        override fun onRemoteAudioStatusUpdated(userId: String, audioStatus: Int, reason: Int, extraInfo: Bundle?) {
            remoteUserId = userId
            // Task 7：正常远端停止只发 UI 事件，绝不 muteRemoteAudio(true)（订阅长期有效，AC-12）
            playback.onRemoteAudioStatusUpdated(userId, audioStatus, reason)
        }
        override fun onError(errCode: Int, errMsg: String, extraInfo: Bundle?) {
            Log.e(TAG, "TRTC error: $errCode $errMsg")
            onError("$errCode", errMsg)
            if (inRoom) onState(ConnectionState.DISCONNECTED) // 进房后错误（SDK 自行重连）
        }
    }

    /** 进房（纯音频 AudioCall 场景）+ 开本地采集上行 + 音量回调。调用方必须先停 MicRecorder（mic handoff）。 */
    fun enterRoom(session: VoiceSessionApi.VoiceSession) {
        if (inRoom) {
            Log.w(TAG, "enterRoom ignored: already in room")
            return
        }
        exitHandled = false
        val params = TRTCCloudDef.TRTCParams().apply {
            sdkAppId = session.sdkAppId
            userId = session.userId
            userSig = session.userSig
            roomId = 0 // 用 strRoomId 时 int 房间号必须为 0（P2-1）
            strRoomId = session.roomId // 字符串房间号（≤64 字节）
        }
        onPhase(VoicePhase.LISTENING)
        onState(ConnectionState.CONNECTING)
        cloud.enterRoom(params, TRTCCloudDef.TRTC_APP_SCENE_AUDIOCALL)
        cloud.startLocalAudio(TRTCCloudDef.TRTC_AUDIO_QUALITY_SPEECH) // 语音档（16k），纯音频不预览
        cloud.enableAudioVolumeEvaluation(
            true,
            TRTCCloudDef.TRTCAudioVolumeEvaluateParams().apply {
                interval = VOLUME_INTERVAL_MS
                enableVadDetection = false
            }
        )
        cloud.setAudioRoute(TRTCCloudDef.TRTC_AUDIO_ROUTE_SPEAKER) // 扬声器外放
        cloud.setAudioFrameListener(audioRms.listener()) // 本地采集帧回调（波形兜底源）
        try { cloud.muteAllRemoteAudio(false) } catch (t: Throwable) { // 进房即取消全部远端静音（防 mute 残留）
            Log.w(TAG, "muteAllRemoteAudio(false) failed: ${t.message}", t)
        }
        scheduleEnterTimeout() // 15s 无 onEnterRoom → 强制失败恢复（防 SDK 吞掉 enterRoom）
        DiagLog.log("Rtc", "enterRoom room=${session.roomId} userId=${session.userId} scene=${session.scene}")
    }

    /** 退房（异步：等 onExitRoom 回调；3s 超时兜底强制恢复）。进房进行中也可退房（取消在途 enter，Task 6）。 */
    fun exitRoom() {
        val pendingEnter = enterTimeoutThread != null
        if (!inRoom && !pendingEnter) {
            Log.w(TAG, "exitRoom ignored: not in room / no pending enter")
            return
        }
        // 进房超时兜底可能已置位 exitHandled；退房是新的完成周期，必须重置（对称于 enterRoom 开头），
        // 否则 onExitRoom/退房兜底的 onExited 全被吞，mic 恢复被迫等 coordinator 5s 退出超时。
        exitHandled = false
        inRoom = false
        cancelEnterTimeout()
        onState(ConnectionState.DISCONNECTED)
        cloud.exitRoom()
        try { cloud.setAudioFrameListener(null) } catch (t: Throwable) {
            Log.w(TAG, "clear audio frame listener failed: ${t.message}", t)
        }
        scheduleExitTimeout()
    }

    /** 是否有在途进房（enterRoom 已调用、onEnterRoom 未回）：coordinator 据此决定是否等待退房回调 */
    fun hasPendingEnter(): Boolean = enterTimeoutThread != null

    /** 显式打断（用户开口/点击，AC-13）：本地播放 stop/flush + generation 失效，长期订阅不变 */
    fun interruptRemotePlayback() {
        val userId = remoteUserId ?: run {
            Log.w(TAG, "interruptRemotePlayback ignored: no remote user")
            return
        }
        playback.interruptPlayback(userId)
    }

    /** 静音/恢复本地上行（继续发静音包）；true=静音 */
    fun muteLocal(muted: Boolean) {
        cloud.muteLocalAudio(muted)
    }

    fun isInRoom(): Boolean = inRoom

    /** 销毁引擎（服务停止时调用；destroySharedInstance 是静态方法，销毁后需重新 sharedInstance） */
    fun release() {
        try {
            cancelExitTimeout()
            cancelEnterTimeout()
            cancelLeaveTimeout()
            cloud.removeListener(listener)
            cloud.setAudioFrameListener(null)
            if (inRoom) {
                inRoom = false
                cloud.exitRoom()
            }
            TRTCCloud.destroySharedInstance()
        } catch (t: Throwable) {
            Log.e(TAG, "release failed: ${t.message}", t)
        }
    }

    private fun scheduleExitTimeout() {
        cancelExitTimeout()
        exitTimeoutThread = daemonDelay(EXIT_TIMEOUT_MS) {
            if (!exitHandled) {
                exitHandled = true
                Log.w(TAG, "onExitRoom timeout (${EXIT_TIMEOUT_MS}ms): forcing onExited")
                onExited()
            }
        }
    }
    private fun cancelExitTimeout() {
        exitTimeoutThread?.interrupt()
        exitTimeoutThread = null
    }
    private fun scheduleEnterTimeout() {
        cancelEnterTimeout()
        enterTimeoutThread = daemonDelay(ENTER_TIMEOUT_MS) {
            if (!exitHandled && !inRoom) {
                exitHandled = true
                inRoom = false
                Log.e(TAG, "onEnterRoom timeout (${ENTER_TIMEOUT_MS}ms): forcing enter failure recovery")
                onState(ConnectionState.DISCONNECTED)
                onError("enter_timeout", "进房超时（${ENTER_TIMEOUT_MS / 1000}s 无回调）")
            }
        }
    }
    private fun cancelEnterTimeout() {
        enterTimeoutThread?.interrupt()
        enterTimeoutThread = null
    }
    private fun scheduleRemoteLeaveTimeout() {
        cancelLeaveTimeout()
        leaveTimeoutThread = daemonDelay(REMOTE_LEAVE_TIMEOUT_MS) {
            Log.w(TAG, "remote leave timeout (${REMOTE_LEAVE_TIMEOUT_MS}ms): auto exitRoom")
            if (inRoom) exitRoom()
        }
    }
    private fun cancelLeaveTimeout() {
        leaveTimeoutThread?.interrupt()
        leaveTimeoutThread = null
    }

    /** 通用守护线程延时兜底：取消 = interrupt（sleep 抛 InterruptedException 后静默退出） */
    private fun daemonDelay(ms: Long, onTimeout: () -> Unit): Thread {
        return Thread {
            try {
                Thread.sleep(ms)
                onTimeout()
            } catch (_: InterruptedException) {
                // 正常回调先到 → 兜底已取消
            }
        }.apply { isDaemon = true; start() }
    }
}
