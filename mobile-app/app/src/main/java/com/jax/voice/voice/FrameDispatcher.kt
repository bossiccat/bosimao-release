package com.jax.voice.voice

/**
 * 采集帧两路分发（v0.6.0 TRTC 重构，spec §4.1 减为两路）：
 * 同一帧 → ① KWS 唤醒检测（wakeEngine 为 null = 唤醒词完全禁用，不碰 sherpa JNI）
 *          ② RMS 音量（悬浮窗/波形绘制）
 *
 * 上行分支已删除：TRTC 方案会话期 mic 由 SDK 独占（mic handoff），不再经 FrameDispatcher 上行；
 * 唤醒命中后由 VoiceForegroundService 停 MicRecorder → 拉会话 → RtcClient.enterRoom。
 */
class FrameDispatcher(
    private val wakeEngine: WakeWordEngine?,
    private val onRms: (Float) -> Unit
) {
    @Volatile
    var wakeEnabled: Boolean = true

    fun onFrame(samples: FloatArray) {
        // ① RMS 音量（悬浮窗/波形绘制）
        onRms(computeRms(samples))

        // ② 唤醒词检测（引擎为空或开关关闭 → 跳过，零 JNI 接触）
        val engine = wakeEngine
        if (wakeEnabled && engine != null) {
            engine.process(samples)
        }

        // VAD（M2 预留）：通话期 mic 被 TRTC 独占，barge-in 判定改走 SDK 回调（§3.4），此处仅监听态预留
    }

    private fun computeRms(samples: FloatArray): Float {
        var sum = 0.0
        for (s in samples) sum += s.toDouble() * s.toDouble()
        return if (samples.isEmpty()) 0f else Math.sqrt(sum / samples.size).toFloat()
    }
}
