package com.jax.voice.net

import com.tencent.trtc.TRTCCloudDef
import com.tencent.trtc.TRTCCloudListener

/**
 * 本地采集音频帧 RMS 计算（Task 7 拆分，原内联于 RtcClient）。
 *
 * 用途（修复「通话中波形不响应」）：onUserVoiceVolume 依赖 SDK 音量评估（300ms 间隔），
 * 在对端尚未进房、评估回调不触发时波形无数据；本回调每帧（~20ms）给出本地采集 PCM，
 * 直接计算 RMS 驱动波形，与 onUserVoiceVolume 互补（本地音量 + 远端音量双源）。
 */
class RtcAudioFrameRms(
    private val onRms: (Float) -> Unit
) {
    private val listener = object : TRTCCloudListener.TRTCAudioFrameListener {
        override fun onCapturedAudioFrame(frame: TRTCCloudDef.TRTCAudioFrame) {
            val rms = computeRms(frame)
            if (rms > 0.003f) onRms(rms) // 静音帧（低于 -50dB）不推，避免波形抖动
        }

        override fun onLocalProcessedAudioFrame(frame: TRTCCloudDef.TRTCAudioFrame) {}
        override fun onRemoteUserAudioFrame(frame: TRTCCloudDef.TRTCAudioFrame, userId: String) {}
        override fun onMixedPlayAudioFrame(frame: TRTCCloudDef.TRTCAudioFrame) {}
        override fun onMixedAllAudioFrame(frame: TRTCCloudDef.TRTCAudioFrame) {}
        override fun onVoiceEarMonitorAudioFrame(frame: TRTCCloudDef.TRTCAudioFrame) {}
    }

    fun listener(): TRTCCloudListener.TRTCAudioFrameListener = listener

    /** 从 TRTCAudioFrame 计算 RMS（0~1）：data 为 byte[] PCM16 小端，16bit 满刻度 32768 */
    fun computeRms(frame: TRTCCloudDef.TRTCAudioFrame): Float {
        val bytes = frame.data ?: return 0f
        if (bytes.size < 2) return 0f
        var sum = 0.0
        var count = 0
        var i = 0
        while (i + 1 < bytes.size) {
            val sample = ((bytes[i].toInt() and 0xFF) or (bytes[i + 1].toInt() shl 8)).toShort().toInt()
            sum += (sample * sample).toDouble()
            count++
            i += 2
        }
        if (count == 0) return 0f
        val rms = Math.sqrt(sum / count)
        return (rms / 32768.0).toFloat().coerceIn(0f, 1f)
    }
}
