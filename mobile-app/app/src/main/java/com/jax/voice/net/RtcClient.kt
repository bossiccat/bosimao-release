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
    /**
     * 本地采集帧检测到「用户开口」；由服务接入 BargeInController.interrupt("user_voice")。
     *
     * 注意语义是「**播放态才打断**」而非「非播放态幂等忽略」：BargeInController 只在
     * SPEAKING/INTERRUPTED 下执行打断，其余状态一律忽略（点击与语音同规则）。
     * 另外语音触发另受「播放起始保护窗 + 每播放段一次」限流，见 BargeInController。
     */
    private val onLocalVoiceActivity: () -> Unit = {},
    /** 真实 SDK onEnterRoom(result >= 0) 后触发，不能用 enterRoom() 同步返回替代。 */
    private val onEntered: () -> Unit = {},
    /** 退房完成回调（onExitRoom 触发；超时兜底也会触发）：调用方在此重启 MicRecorder 恢复"一直在听" */
    private val onExited: () -> Unit,
    /** 远端播放 UI 事件（Task 7：正常停止 = RemoteAudioStopped，Task 8 接入 BargeInController） */
    private val onRemoteAudioEvent: (RtcPlaybackSubscription.RemoteAudioEvent) -> Unit = {},
    /** 引擎工厂（测试注入，QA L0 RTC-CLIENT-TEST-DESIGN §2）：默认 TRTCCloud.sharedInstance(appContext) */
    private val engineFactory: (Context) -> TRTCCloud = { ctx -> TRTCCloud.sharedInstance(ctx) },
    /** 会话期自定义采集源（测试注入 fake；默认 RealCustomAudioSource 进程内单例：MIC+平台AEC+sendCustomAudioData） */
    private val customAudioSource: CustomAudioSource = RealCustomAudioSource.get()
) {
    /**
     * 实例标识（真机取证）：TRTCCloud 是进程级单例，多个 RtcClient 会把 listener 挂到同一个
     * 引擎上并各自收到回调。没有这个 id，日志里无法分清是哪一代 client 出的错。
     */
    private val instanceId: String = "rtc#${SEQ.incrementAndGet()}"

    companion object {
        private val SEQ = java.util.concurrent.atomic.AtomicInteger(0)
        private const val TAG = "RtcClient"
        // 常量（MOBILE-INTEGRATION §1.3 / P2-2 / v0.6.2 / P2-3）
        private const val VOLUME_INTERVAL_MS = 300 // 音量回调间隔
        private const val EXIT_TIMEOUT_MS = 3_000L // 退房回调超时兜底
        private const val ENTER_TIMEOUT_MS = 15_000L // 进房回调超时兜底
        private const val REMOTE_LEAVE_TIMEOUT_MS = 60_000L // 对端离开超时退房
        /** 自定义命令 cmdId=1：会话终止通知（与 sidecar rtc.js onRecvCustomCmdMsg 约定一致，Task #23） */
        const val CMD_ID_TERMINATE = 1
    }

    /**
     * 会话期自定义音频源（2026-09-05 回音/误打断根治）：进房成功后由 RtcClient 驱动启停，
     * 真实实现 [RealCustomAudioSource]（AudioSource.MIC + Android AEC/NS + sendCustomAudioData）。
     * 启动失败不阻断进房状态（采集缺失只影响上行，真机日志留痕）。
     */
    interface CustomAudioSource {
        fun start(cloud: TRTCCloud): Boolean
        fun stop()
    }

    @Volatile private var inRoom = false // 本地维护；13.4 SDK 无 isInRoom 公开方法
    @Volatile private var exitHandled = false // 防「超时兜底 + 真实回调」双触发
    @Volatile private var lastVolLogTs = 0L // 非零音量降频记录（3s 一条）
    @Volatile private var remoteUserId: String? = null // 最近远端用户（打断 flush 目标）

    private val audioRms = RtcAudioFrameRms(
        onRms = { onRms(it) },
        onVoiceActivity = { onLocalVoiceActivity() }
    ) // 本地采集帧 RMS（波形兜底源）

    private val timeouts = RtcClientTimeouts(
        exitTimeoutMs = EXIT_TIMEOUT_MS,
        enterTimeoutMs = ENTER_TIMEOUT_MS,
        remoteLeaveTimeoutMs = REMOTE_LEAVE_TIMEOUT_MS,
        onExitTimeout = {
            if (!exitHandled) {
                exitHandled = true
                Log.w(TAG, "onExitRoom timeout (${EXIT_TIMEOUT_MS}ms): forcing onExited")
                onExited()
            }
        },
        onEnterTimeout = {
            if (!exitHandled && !inRoom) {
                exitHandled = true
                inRoom = false
                Log.e(TAG, "onEnterRoom timeout (${ENTER_TIMEOUT_MS}ms): forcing enter failure recovery [$instanceId]")
                // P1-2：进房超时不代表采集没起来（onEnterRoom 回调与超时可能竞态），必须显式停采集
                try { customAudioSource.stop() } catch (t: Throwable) {
                    Log.w(TAG, "custom audio source stop failed: ${t.message}", t)
                }
                onState(ConnectionState.DISCONNECTED)
                onError("enter_timeout", "进房超时（${ENTER_TIMEOUT_MS / 1000}s 无回调）")
            }
        },
        onRemoteLeaveTimeout = {
            Log.w(TAG, "remote leave timeout (${REMOTE_LEAVE_TIMEOUT_MS}ms): auto exitRoom")
            if (inRoom) exitRoom()
        }
    )

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
            DiagLog.log("Rtc", "onEnterRoom result=$result [$instanceId]")
            // 判成功：真实 SDK result>0=成功（耗时ms）、result<0=失败；result==0 为测试 mock 成功哨兵
            if (result >= 0) {
                inRoom = true
                VoiceController.setLastError("")
                // 采集源切换（2026-09-05 回音/误打断根治）：自定义采集替代 MUSIC 档内部采集。
                // 背景：SPEECH 档 VOICE_COMMUNICATION 源在本机（Samsung S26U，AGM LPI 路径）送出全零，
                // MUSIC 档（MIC 源）有声但无 AEC/NS → 千问回复外放被采回 → 误 barge-in 截断回复
                // （bridge dropped=3/4）。自定义采集走 MIC 源 + Android AcousticEchoCanceler。
                try {
                    cloud.enableCustomAudioCapture(true)
                } catch (t: Throwable) {
                    Log.e(TAG, "enableCustomAudioCapture(true) failed: ${t.message}", t)
                }
                val srcOk = try { customAudioSource.start(cloud) } catch (t: Throwable) {
                    Log.e(TAG, "custom audio source start failed: ${t.message}", t); false
                }
                DiagLog.log("Rtc", "custom capture start ok=$srcOk")
                onState(ConnectionState.CONNECTED)
                onEntered()
            } else {
                inRoom = false
                onState(ConnectionState.DISCONNECTED)
                onError("enter_room", "进房失败: $result")
            }
        }
        override fun onExitRoom(reason: Int) {
            Log.i(TAG, "onExitRoom reason=$reason (0主动退出/1被踢/2房间解散) [$instanceId]")
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
        override fun onRecvCustomCmdMsg(userId: String, cmdId: Int, seq: Int, message: ByteArray?) {
            // 反向命令预留（Task #23）：sidecar → 手机下行自定义命令通道；当前只记录，不处理。
            DiagLog.log("Rtc", "onRecvCustomCmdMsg user=$userId cmdId=$cmdId seq=$seq len=${message?.size ?: 0}")
        }
        override fun onError(errCode: Int, errMsg: String, extraInfo: Bundle?) {
            Log.e(TAG, "TRTC error: $errCode $errMsg [$instanceId]")
            onError("$errCode", errMsg)
            if (inRoom) onState(ConnectionState.DISCONNECTED) // 进房后错误（SDK 自行重连）
        }
    }

    /** 进房（纯音频 AudioCall 场景）+ 开本地采集上行 + 音量回调。调用方必须先停 MicRecorder（mic handoff）。 */
    fun enterRoom(session: VoiceSessionApi.VoiceSession) {
        if (inRoom) {
            Log.w(TAG, "enterRoom ignored: already in room [$instanceId]")
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
        // 采集源契约（2026-09-05 定稿）：上行 = 自定义采集（onEnterRoom 成功后启用）。
        // 历史：SPEECH 档（VOICE_COMMUNICATION 源）在本机送全零 → MUSIC 档（MIC 源）有声音
        // 但无 AEC/NS（回音根因）→ 现改为 AudioSource.MIC + 平台 AEC 的自定义采集
        // （RealCustomAudioSource），不再调用 startLocalAudio。
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
        Log.i(TAG, "enterRoom room=${session.roomId} strRoomId=${session.roomId} [$instanceId]")
        DiagLog.log("Rtc", "enterRoom room=${session.roomId} userId=${session.userId} scene=${session.scene} [$instanceId]")
    }

    /** 退房（异步：等 onExitRoom 回调；3s 超时兜底强制恢复）。进房进行中也可退房（取消在途 enter，Task 6）。 */
    fun exitRoom() {
        val pendingEnter = timeouts.enterThread != null
        if (!inRoom && !pendingEnter) {
            Log.w(TAG, "exitRoom ignored: not in room / no pending enter [$instanceId]")
            // P1-2：早退分支同样要保证自定义采集已停，否则退房路径漏掉 stop 会留下采集线程。
            // 不碰 cloud（lazy）——本分支意味着从未成功进房，也就从未 enableCustomAudioCapture(true)。
            try { customAudioSource.stop() } catch (t: Throwable) {
                Log.w(TAG, "custom audio source stop failed: ${t.message}", t)
            }
            return
        }
        Log.i(TAG, "exitRoom [$instanceId] inRoom=$inRoom pendingEnter=$pendingEnter")
        // 进房超时兜底可能已置位 exitHandled；退房是新的完成周期，必须重置（对称于 enterRoom 开头），
        // 否则 onExitRoom/退房兜底的 onExited 全被吞，mic 恢复被迫等 coordinator 5s 退出超时。
        exitHandled = false
        inRoom = false
        cancelEnterTimeout()
        onState(ConnectionState.DISCONNECTED)
        // 自定义采集对称关闭（2026-09-05）：先停源（停止 read/send），再关 SDK 自定义采集
        try { customAudioSource.stop() } catch (t: Throwable) {
            Log.w(TAG, "custom audio source stop failed: ${t.message}", t)
        }
        try { cloud.enableCustomAudioCapture(false) } catch (t: Throwable) {
            Log.w(TAG, "enableCustomAudioCapture(false) failed: ${t.message}", t)
        }
        cloud.exitRoom()
        try { cloud.setAudioFrameListener(null) } catch (t: Throwable) {
            Log.w(TAG, "clear audio frame listener failed: ${t.message}", t)
        }
        scheduleExitTimeout()
    }

    /** 是否有在途进房（enterRoom 已调用、onEnterRoom 未回）：coordinator 据此决定是否等待退房回调 */
    fun hasPendingEnter(): Boolean = timeouts.enterThread != null

    /**
     * 退房前向 sidecar 发终止通知（sendCustomCmdMsg，cmdId=[CMD_ID_TERMINATE]，reliable+ordered）。
     * payload JSON {type:"note_termination", termination_id}，sidecar rtc.js 解析后经 bridge
     * noteTermination 中继给 rtc_bridge。绝不抛错：SDK 异常/未进房一律返回 false（调用方可短重试）。
     */
    fun sendTerminationNotice(terminationId: String): Boolean {
        if (terminationId.isBlank()) {
            Log.w(TAG, "sendTerminationNotice ignored: blank terminationId")
            return false
        }
        val payload = org.json.JSONObject()
            .put("type", "note_termination")
            .put("termination_id", terminationId)
            .toString()
            .toByteArray(Charsets.UTF_8)
        return try {
            // javap 核对 13.4.0.20477 jar：sendCustomCmdMsg(int cmdId, byte[] data, boolean reliable, boolean ordered)
            val sent = cloud.sendCustomCmdMsg(CMD_ID_TERMINATE, payload, true, true)
            DiagLog.log("Rtc", "terminationNotice sent=$sent tid=$terminationId")
            sent
        } catch (t: Throwable) {
            Log.w(TAG, "sendTerminationNotice failed: ${t.message}", t)
            false
        }
    }

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

    /**
     * 自定义采集上行帧（16k/mono/20ms=320样本→640B PCM16LE，SPEC §4.1/AC-08 契约）。
     * 未进房静默忽略（防退房竞态期残留帧发送）；格式与 RealCustomAudioSource 直发一致。
     */
    fun sendCustomAudioFrame(samples: ShortArray) {
        if (!inRoom) return
        // javap 核对 13.4.0.20477：TRTCAudioFrame 仅 data/sampleRate/channel/timestamp/extraData
        val frame = TRTCCloudDef.TRTCAudioFrame()
        frame.data = RtcCustomAudioPcm.shortToPcm16le(samples)
        frame.sampleRate = RtcCustomAudioPcm.SAMPLE_RATE
        frame.channel = 1
        frame.timestamp = System.currentTimeMillis()
        cloud.sendCustomAudioData(frame)
    }

    fun isInRoom(): Boolean = inRoom

    /** 销毁引擎（服务停止时调用；destroySharedInstance 是静态方法，销毁后需重新 sharedInstance） */
    fun release() {
        try {
            cancelExitTimeout()
            cancelEnterTimeout()
            cancelLeaveTimeout()
            try { customAudioSource.stop() } catch (_: Throwable) {}
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

    private fun scheduleExitTimeout() = timeouts.scheduleExit()
    private fun cancelExitTimeout() = timeouts.cancelExit()
    private fun scheduleEnterTimeout() = timeouts.scheduleEnter()
    private fun cancelEnterTimeout() = timeouts.cancelEnter()
    private fun scheduleRemoteLeaveTimeout() = timeouts.scheduleLeave()
    private fun cancelLeaveTimeout() = timeouts.cancelLeave()
}
