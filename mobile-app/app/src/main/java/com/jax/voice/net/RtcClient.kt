package com.jax.voice.net

import android.content.Context
import android.os.Bundle
import android.util.Log
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoicePhase
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudDef
import com.tencent.trtc.TRTCCloudListener

/**
 * TRTC 通话客户端 —— 纯音频 1v1 通话；替代 VoiceWsClient（WS+配对）。
 *
 * 依据：docs/rtc-rebuild/MOBILE-INTEGRATION.md §2/§3.4（实施基准）/ ARCHITECTURE.md §5.1 / ADR-012。
 * API 签名对照官方 TRTC Android SDK 13.4.0.20477 实际 jar（javap 核对，非记忆）：
 * - TRTCCloud.sharedInstance(ctx) / addListener / enterRoom(TRTCParams, scene) / exitRoom / startLocalAudio
 * - 回调：onEnterRoom(long)（>0 成功=耗时ms）、onExitRoom(int)、onRemoteUserEnterRoom/LeaveRoom、
 *   onUserVoiceVolume(ArrayList<TRTCVolumeInfo>, totalVolume)、onConnectionLost/onTryToReconnect/onConnectionRecovery、
 *   onRemoteAudioStatusUpdated（13.4 名，非旧文档 onRemoteUserAudioStatus）、onError(int,String,Bundle)、onMicDidReady。
 *
 * 设计要点：
 * - 仅会话期进房（KWS 唤醒 → 拉 roomId+userSig → enterRoom；对话结束 exitRoom），常驻监听不耗 RTC 分钟。
 * - mic handoff：enterRoom 前调用方必须已停 MicRecorder（Android 不允许双 AudioRecord 同时采集）；
 *   exitRoom 需等 onExitRoom 回调后再重启 MicRecorder（ADR-012 手机端细节 3），由 onExited 回调通知；
 *   **兜底**：exitRoom 后 3s 未收到 onExitRoom → 强制触发 onExited（防回调丢失永不恢复，P2-2）。
 * - 对端离开：60s 未重进 → 自动退房（防持续耗 RTC 分钟，P2-3）。
 * - 断线重连 SDK 内置（无限重连），应用层只把 onConnectionLost/Recovery 映射到六态 UI；
 *   onConnectionLost 先置 DISCONNECTED 中间态再 CONNECTING（QA-PLAN §2 A，P2-4）。
 * - 打断状态机（QA-PLAN §3.3 / MOBILE-INTEGRATION §3.4，P1-1）：会话期本地 VAD 不参与（mic 被 TRTC 独占），
 *   onRemoteAudioStatusUpdated 驱动六态：远端说话 → SPEAKING；远端静音/停止（回复结束/打断）→
 *   停播下行（muteRemoteAudio 兜底）+ 切 LISTENING，保证「UI 状态与音频停止一致」。
 * - 播放：MVP 走 SDK 自动播放（自动订阅，远端音频自动解码播放），不注册 onAudioFrame 接管。
 * - 加密：默认 DTLS-SRTP 传输加密（SecretKey 唯一存云函数环境变量，userSig 短时效，App 不持有密钥）。
 */
class RtcClient(
    private val appContext: Context,
    private val onState: (ConnectionState) -> Unit,
    private val onPhase: (VoicePhase) -> Unit,
    private val onRms: (Float) -> Unit,
    private val onError: (code: String, msg: String) -> Unit,
    /** 退房完成回调（onExitRoom 触发；超时兜底也会触发）：调用方在此重启 MicRecorder 恢复"一直在听" */
    private val onExited: () -> Unit,
    /**
     * 引擎工厂（测试注入，QA L0 RTC-CLIENT-TEST-DESIGN §2）：默认 TRTCCloud.sharedInstance(appContext)
     * 生产单例；qa 测试传 FakeRtcEngine（extends TRTCCloud 覆盖同签名方法，不连真实 RTC 云）。
     */
    private val engineFactory: (Context) -> TRTCCloud = { ctx -> TRTCCloud.sharedInstance(ctx) }
) {
    companion object {
        private const val TAG = "RtcClient"
        /** 音量回调间隔 300ms（MOBILE-INTEGRATION §1.3）；第二参 enableVad=false（波形需要连续音量） */
        private const val VOLUME_INTERVAL_MS = 300
        /** 退房回调超时兜底：exitRoom 后 3s 内未收到 onExitRoom → 强制恢复 MicRecorder（P2-2） */
        private const val EXIT_TIMEOUT_MS = 3_000L
        /** 对端离开超时退房：60s 内未重进 → 自动退房（防持续耗 RTC 分钟，P2-3） */
        private const val REMOTE_LEAVE_TIMEOUT_MS = 60_000L
        /**
         * TRTC onRemoteAudioStatusUpdated 的 audioStatus 取值（SDK 13.4 语义，QA-PLAN §3.3）：
         * 1=远端有音频（说话中 / TRTCAudioStatusSpeaking）；2=远端静音/停止（打断结束 / TRTCAudioStatusListening）。
         */
        private const val AUDIO_STATUS_SPEAKING = 1
        private const val AUDIO_STATUS_LISTENING = 2
    }

    /** 是否已在房（本地维护；13.4 SDK 无 isInRoom 公开方法） */
    @Volatile
    private var inRoom = false

    /** 本次退房是否已触发过 onExited（防「超时兜底 + 真实回调」双触发） */
    @Volatile
    private var exitHandled = false

    /** 退房超时兜底线程（onExitRoom 到达或新一次退房/释放时取消） */
    @Volatile
    private var exitTimeoutThread: Thread? = null

    /** 对端离开超时退房线程（对端重进或退房/释放时取消） */
    @Volatile
    private var leaveTimeoutThread: Thread? = null

    /** TRTC 引擎（默认 App 进程级单例 sharedInstance）；懒加载：首次 enterRoom 才创建实例 */
    private val cloud: TRTCCloud by lazy {
        engineFactory(appContext).also { it.addListener(listener) }
    }

    private val listener = object : TRTCCloudListener() {

        override fun onEnterRoom(result: Long) {
            // 判成功：真实 SDK result>0=成功（耗时ms）、result<0=失败（错误码）；result==0 为测试 mock
            // 的成功哨兵（RTC-CLIENT-TEST-DESIGN §2.1 S1，真实 SDK 成功必>0，0 不会出现），统一按 >=0 判成功。
            if (result >= 0) {
                // 进房成功 → CONNECTED，清错误
                inRoom = true
                VoiceController.setLastError("")
                onState(ConnectionState.CONNECTED)
            } else {
                // 进房失败（result < 0 = 错误码）→ DISCONNECTED + 错误
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
            // 远端（PC sidecar）进房 = 可通话；清除"对端已退出"提示
            cancelLeaveTimeout()
            VoiceController.setLastError("")
            onState(ConnectionState.CONNECTED)
            onPhase(VoicePhase.LISTENING)
        }

        override fun onRemoteUserLeaveRoom(userId: String, reason: Int) {
            // 对端退出：提示 + 保持房间等对端重进；60s 未重进 → 自动退房（P2-3，防持续耗 RTC 分钟）
            VoiceController.setLastError("对端已退出")
            onPhase(VoicePhase.LISTENING)
            scheduleRemoteLeaveTimeout()
        }

        override fun onFirstAudioFrame(userId: String) {
            // 远端首帧音频 = 已可播放
            onPhase(VoicePhase.LISTENING)
        }

        override fun onUserVoiceVolume(userVolumes: ArrayList<TRTCCloudDef.TRTCVolumeInfo>, totalVolume: Int) {
            // totalVolume 0~100 → 归一化 0~1 驱动悬浮窗波形（本地+远端合计音量）
            onRms(totalVolume / 100f)
        }

        override fun onConnectionLost() {
            // 断连（约连续 8s 未连上）→ 先 DISCONNECTED 中间态，再 CONNECTING（QA-PLAN §2 A，P2-4）
            VoiceController.setLastError("网络中断，重连中…")
            onState(ConnectionState.DISCONNECTED)
            onState(ConnectionState.CONNECTING)
        }

        override fun onTryToReconnect() {
            // 断连 3s 后开始尝试（之后每 24s 重试）→ CONNECTING
            onState(ConnectionState.CONNECTING)
        }

        override fun onConnectionRecovery() {
            // 重连成功 → CONNECTED，清错误
            VoiceController.setLastError("")
            onState(ConnectionState.CONNECTED)
        }

        override fun onRemoteAudioStatusUpdated(userId: String, audioStatus: Int, reason: Int, extraInfo: Bundle?) {
            // P1-1 打断状态机（QA-PLAN §3.3 / MOBILE-INTEGRATION §3.4）：
            // 会话期 mic 被 TRTC 独占、本地 VAD 不参与打断，打断判定来源 = 本回调。
            // 远端说话 → 六态 SPEAKING（对话中）；远端静音/停止（回复结束/打断）→ 停播下行 + 切 LISTENING，
            // 保证「UI 状态与音频停止一致」（不允许音频已停但 UI 还停在 Speaking）。
            Log.d(TAG, "remote audio status userId=$userId audioStatus=$audioStatus reason=$reason")
            when (audioStatus) {
                AUDIO_STATUS_SPEAKING -> onPhase(VoicePhase.SPEAKING)

                AUDIO_STATUS_LISTENING -> {
                    // 下行停播兜底（SDK 自动播放时 muteRemoteAudio 停掉对端音频，不依赖 playGen）
                    try {
                        cloud.muteRemoteAudio(userId, true)
                    } catch (t: Throwable) {
                        Log.w(TAG, "muteRemoteAudio failed: ${t.message}", t)
                    }
                    onPhase(VoicePhase.LISTENING)
                }

                else -> Log.d(TAG, "unknown audio status=$audioStatus")
            }
        }

        override fun onMicDidReady() {
            Log.i(TAG, "mic ready")
        }

        override fun onError(errCode: Int, errMsg: String, extraInfo: Bundle?) {
            Log.e(TAG, "TRTC error: $errCode $errMsg")
            onError("$errCode", errMsg)
            if (inRoom) {
                // 进房后错误：置 DISCONNECTED（SDK 会自行处理重连；错误码如 -3317 等）
                onState(ConnectionState.DISCONNECTED)
            }
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
            // P2-1：显式置 0（用 strRoomId 时 int 房间号必须为 0；SDK 字段名是 roomId，非 ADR 旧称 intRoomId）
            roomId = 0
            // 字符串房间号（≤64 字节）；用 strRoomId 时 roomId 必须为 0（ADR-012 实施补充）
            strRoomId = session.roomId
        }
        // 进房 = 会话开始：手机处于"已唤醒，在听"（对端说话后再由音频状态切 SPEAKING）
        onPhase(VoicePhase.LISTENING)
        onState(ConnectionState.CONNECTING)
        cloud.enterRoom(params, TRTCCloudDef.TRTC_APP_SCENE_AUDIOCALL)
        // 语音档（16k）与现有 16k 采集链路一致；不调用 startLocalPreview = 纯音频
        cloud.startLocalAudio(TRTCCloudDef.TRTC_AUDIO_QUALITY_SPEECH)
        // 音量回调（13.4 签名：enable + TRTCAudioVolumeEvaluateParams{interval, enableVadDetection}）
        cloud.enableAudioVolumeEvaluation(
            true,
            TRTCCloudDef.TRTCAudioVolumeEvaluateParams().apply {
                interval = VOLUME_INTERVAL_MS
                enableVadDetection = false
            }
        )
        // 扬声器外放
        cloud.setAudioRoute(TRTCCloudDef.TRTC_AUDIO_ROUTE_SPEAKER)
        Log.i(TAG, "enterRoom room=${session.roomId} userId=${session.userId} scene=${session.scene}")
    }

    /** 退房（异步：等 onExitRoom 回调，调用方在 onExited 中重启 MicRecorder；3s 超时兜底强制恢复） */
    fun exitRoom() {
        if (!inRoom) {
            Log.w(TAG, "exitRoom ignored: not in room")
            return
        }
        inRoom = false
        onState(ConnectionState.DISCONNECTED)
        cloud.exitRoom()
        scheduleExitTimeout()
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
            cancelLeaveTimeout()
            cloud.removeListener(listener)
            if (inRoom) {
                inRoom = false
                cloud.exitRoom()
            }
            TRTCCloud.destroySharedInstance()
        } catch (t: Throwable) {
            Log.e(TAG, "release failed: ${t.message}", t)
        }
    }

    /** P2-2：退房后 3s 未收到 onExitRoom → 强制恢复 MicRecorder（防回调丢失永不恢复） */
    private fun scheduleExitTimeout() {
        cancelExitTimeout()
        val t = Thread {
            try {
                Thread.sleep(EXIT_TIMEOUT_MS)
                if (!exitHandled) {
                    exitHandled = true
                    Log.w(TAG, "onExitRoom timeout (${EXIT_TIMEOUT_MS}ms): forcing onExited")
                    onExited()
                }
            } catch (_: InterruptedException) {
                // 正常 onExitRoom 先到 → 兜底已取消
            }
        }.apply { isDaemon = true; start() }
        exitTimeoutThread = t
    }

    private fun cancelExitTimeout() {
        exitTimeoutThread?.interrupt()
        exitTimeoutThread = null
    }

    /** P2-3：对端离开后 60s 未重进 → 自动退房（防持续耗 RTC 分钟） */
    private fun scheduleRemoteLeaveTimeout() {
        cancelLeaveTimeout()
        val t = Thread {
            try {
                Thread.sleep(REMOTE_LEAVE_TIMEOUT_MS)
                Log.w(TAG, "remote leave timeout (${REMOTE_LEAVE_TIMEOUT_MS}ms): auto exitRoom")
                if (inRoom) exitRoom()
            } catch (_: InterruptedException) {
                // 对端已重进 / 已退房 → 取消
            }
        }.apply { isDaemon = true; start() }
        leaveTimeoutThread = t
    }

    private fun cancelLeaveTimeout() {
        leaveTimeoutThread?.interrupt()
        leaveTimeoutThread = null
    }
}
