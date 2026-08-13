package com.jax.voice.net

import android.util.Log
import com.jax.voice.util.DiagLog
import com.jax.voice.voice.VoicePhase
import com.tencent.trtc.TRTCCloud

/**
 * 远端播放订阅与打断（Task 7 / SPEC AC-12 AC-13 AC-14）。
 *
 * 历史根因：onRemoteAudioStatusUpdated(audioStatus=2) 调用 muteRemoteAudio(true) 停播下行，
 * 恢复依赖下一次 SPEAKING 事件对称 unmute——事件丢失/时序偏移即导致远端被永久静音，
 * 第二轮回复开始后手机无声（sidecar 下行帧正常、APM 正常回复）。
 *
 * 新语义（Task 7）：
 * - 正常远端停止（audioStatus=2）只发布 [RemoteAudioEvent.STOPPED] UI 事件并切 LISTENING，
 *   绝不调用 muteRemoteAudio(true)——SDK 自动订阅保持长期有效，第二轮无需恢复订阅即可收到帧（AC-12）。
 * - 显式打断（用户开口/点击，AC-13）只做本地播放 stop/flush 脉冲 + generation 失效（AC-14）：
 *   mute(true) 停播并冲刷本地缓冲，随后立即 mute(false) 恢复订阅；打断不改变长期远端订阅。
 * - [ensureUnmuted] 仅在远端进房/首帧/音频可用等兜底路径防御性解除静音，不参与正常状态机。
 */
class RtcPlaybackSubscription(
    private val cloud: () -> TRTCCloud,
    private val onPhase: (VoicePhase) -> Unit,
    private val onUiEvent: (RemoteAudioEvent) -> Unit = {}
) {

    /** 远端播放 UI 事件（Task 8 接入 BargeInController/VoiceUiModel 消费） */
    enum class RemoteAudioEvent { STARTED, STOPPED }

    companion object {
        private const val TAG = "RtcPlayback"
        /** TRTC onRemoteAudioStatusUpdated 的 audioStatus（SDK 13.4 语义）：1=远端说话中 */
        private const val AUDIO_STATUS_SPEAKING = 1
        /** 2=远端静音/停止（回复结束，非打断） */
        private const val AUDIO_STATUS_LISTENING = 2
    }

    /** 播放代数：显式打断时递增；旧 generation 下行帧到达视为过期（AC-14） */
    @Volatile
    var playbackGeneration: Int = 0
        private set

    /**
     * 正常远端状态回调：只发布 UI 事件 + 阶段，绝不触碰 muteRemoteAudio（AC-12）。
     * audioStatus=1 说话中 → STARTED + SPEAKING；audioStatus=2 停止 → STOPPED + LISTENING。
     */
    fun onRemoteAudioStatusUpdated(userId: String, audioStatus: Int, reason: Int) {
        when (audioStatus) {
            AUDIO_STATUS_SPEAKING -> {
                DiagLog.log("Rtc", "audioStatus SPEAKING user=$userId reason=$reason -> UI STARTED (no mute)")
                onUiEvent(RemoteAudioEvent.STARTED)
                onPhase(VoicePhase.SPEAKING)
            }
            AUDIO_STATUS_LISTENING -> {
                DiagLog.log("Rtc", "audioStatus LISTENING user=$userId reason=$reason -> UI STOPPED (no mute)")
                onUiEvent(RemoteAudioEvent.STOPPED)
                onPhase(VoicePhase.LISTENING)
            }
            else -> Log.d(TAG, "unknown audio status=$audioStatus reason=$reason")
        }
    }

    /** 远端进房：切 LISTENING + 防御性解除静音（防上一会话 mute 残留，订阅长期不变） */
    fun onRemoteUserEnterRoom(userId: String) {
        DiagLog.log("Rtc", "remoteEnter user=$userId -> ensureUnmuted")
        ensureUnmuted(userId)
        onPhase(VoicePhase.LISTENING)
    }

    /** 远端首帧音频：可播放，防御性解除静音 */
    fun onFirstAudioFrame(userId: String) {
        DiagLog.log("Rtc", "firstAudioFrame user=$userId -> ensureUnmuted")
        ensureUnmuted(userId)
        onPhase(VoicePhase.LISTENING)
    }

    /** 显式打断（AC-13）：本地播放 stop/flush 脉冲 + generation 失效；脉冲后恢复订阅（AC-14） */
    fun interruptPlayback(userId: String) {
        playbackGeneration++
        DiagLog.log("Rtc", "interruptPlayback user=$userId gen=$playbackGeneration (local stop/flush)")
        try {
            cloud().muteRemoteAudio(userId, true) // 本地停播并冲刷缓冲
            cloud().muteRemoteAudio(userId, false) // 立即恢复，长期订阅不变
        } catch (t: Throwable) {
            Log.w(TAG, "interrupt flush failed: ${t.message}", t)
        }
        onPhase(VoicePhase.LISTENING)
    }

    /** 防御性解除静音（远端进房/首帧/音频可用兜底）；正常状态机不调用 */
    fun ensureUnmuted(userId: String) {
        try {
            cloud().muteRemoteAudio(userId, false)
        } catch (t: Throwable) {
            Log.w(TAG, "ensureUnmuted failed: ${t.message}", t)
        }
    }
}
