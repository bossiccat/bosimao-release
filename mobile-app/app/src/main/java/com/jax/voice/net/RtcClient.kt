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
 * 依据：docs/rtc-rebuild/MOBILE-INTEGRATION.md §2（实施基准）/ ARCHITECTURE.md §5.1 / ADR-012。
 * API 签名对照官方 TRTC Android SDK 13.4.0.20477 实际 jar（javap 核对，非记忆）：
 * - TRTCCloud.sharedInstance(ctx) / addListener / enterRoom(TRTCParams, scene) / exitRoom / startLocalAudio
 * - 回调：onEnterRoom(long)（>0 成功=耗时ms）、onExitRoom(int)、onRemoteUserEnterRoom/LeaveRoom、
 *   onUserVoiceVolume(ArrayList<TRTCVolumeInfo>, totalVolume)、onConnectionLost/onTryToReconnect/onConnectionRecovery、
 *   onRemoteAudioStatusUpdated（13.4 名，非旧文档 onRemoteUserAudioStatus）、onError(int,String,Bundle)、onMicDidReady。
 *
 * 设计要点：
 * - 仅会话期进房（KWS 唤醒 → 拉 roomId+userSig → enterRoom；对话结束 exitRoom），常驻监听不耗 RTC 分钟。
 * - mic handoff：enterRoom 前调用方必须已停 MicRecorder（Android 不允许双 AudioRecord 同时采集）；
 *   exitRoom 需等 onExitRoom 回调后再重启 MicRecorder（ADR-012 手机端细节 3），由 onExited 回调通知。
 * - 断线重连 SDK 内置（无限重连），应用层只把 onConnectionLost/Recovery 映射到六态 UI。
 * - 播放：MVP 走 SDK 自动播放（自动订阅，远端音频自动解码播放），不注册 onAudioFrame 接管。
 * - 加密：默认 DTLS-SRTP 传输加密（SecretKey 唯一存云函数环境变量，userSig 短时效，App 不持有密钥）。
 */
class RtcClient(
    private val appContext: Context,
    private val onState: (ConnectionState) -> Unit,
    private val onPhase: (VoicePhase) -> Unit,
    private val onRms: (Float) -> Unit,
    private val onError: (code: String, msg: String) -> Unit,
    /** 退房完成回调（onExitRoom 触发）：调用方在此重启 MicRecorder 恢复"一直在听" */
    private val onExited: () -> Unit
) {
    companion object {
        private const val TAG = "RtcClient"
        /** 音量回调间隔 300ms（MOBILE-INTEGRATION §1.3）；第二参 enableVad=false（波形需要连续音量） */
        private const val VOLUME_INTERVAL_MS = 300
    }

    /** 是否已在房（本地维护；13.4 SDK 无 isInRoom 公开方法） */
    @Volatile
    private var inRoom = false

    /** App 进程级单例（TRTCCloud.sharedInstance）；懒加载：首次 enterRoom 才创建实例 */
    private val cloud: TRTCCloud by lazy {
        TRTCCloud.sharedInstance(appContext).also { it.addListener(listener) }
    }

    private val listener = object : TRTCCloudListener() {

        override fun onEnterRoom(result: Long) {
            if (result > 0) {
                // 进房成功（result = 耗时 ms）→ CONNECTED，清错误
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
            inRoom = false
            onState(ConnectionState.DISCONNECTED)
            onExited()
        }

        override fun onRemoteUserEnterRoom(userId: String) {
            // 远端（PC sidecar）进房 = 可通话
            onState(ConnectionState.CONNECTED)
            onPhase(VoicePhase.LISTENING)
        }

        override fun onRemoteUserLeaveRoom(userId: String, reason: Int) {
            // 对端退出：提示 + 保持房间等待（或按产品自动退房）；MVP 保持房间等对端重进
            VoiceController.setLastError("对端已退出")
            onPhase(VoicePhase.LISTENING)
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
            // 断连（约连续 8s 未连上）→ CONNECTING + 提示（SDK 自动重连）
            VoiceController.setLastError("网络中断，重连中…")
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
            // 远端是否在说话（barge-in 状态机 / 六态 Speaking→Listening 判定；Phase B 完善，MVP 仅记日志）
            Log.d(TAG, "remote audio status userId=$userId audioStatus=$audioStatus reason=$reason")
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
        val params = TRTCCloudDef.TRTCParams().apply {
            sdkAppId = session.sdkAppId
            userId = session.userId
            userSig = session.userSig
            // 字符串房间号（≤64 字节）；用 strRoomId 时 intRoomId 必须为 0（ADR-012 实施补充）
            strRoomId = session.roomId
        }
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

    /** 退房（异步：等 onExitRoom 回调，调用方在 onExited 中重启 MicRecorder） */
    fun exitRoom() {
        if (!inRoom) {
            Log.w(TAG, "exitRoom ignored: not in room")
            return
        }
        inRoom = false
        onState(ConnectionState.DISCONNECTED)
        cloud.exitRoom()
    }

    /** 静音/恢复本地上行（继续发静音包）；true=静音 */
    fun muteLocal(muted: Boolean) {
        cloud.muteLocalAudio(muted)
    }

    fun isInRoom(): Boolean = inRoom

    /** 销毁引擎（服务停止时调用；destroySharedInstance 是静态方法，销毁后需重新 sharedInstance） */
    fun release() {
        try {
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
}
