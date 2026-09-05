package com.jax.voice.net

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudDef
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 会话期自定义音频采集源（2026-09-05 回音/误打断根治，替代 MUSIC 档内部采集）：
 *
 * 背景（真机实锤）：
 *  - TRTC 内部 SPEECH 档绑 VOICE_COMMUNICATION 源，Samsung S26U（AGM VoiceActivation/LPI 路径）
 *    实测送出全零 → 被迫切 MUSIC 档（MIC 源，有声），但 MUSIC 档无 SDK AEC/NS；
 *  - 后果：千问回复经手机扬声器外放被 mic 采回 → 千问听到自己 → 误 barge-in 截断回复
 *    （bridge 日志 dropped=3/4），且用户自声经 PC 外放形成回环。
 *
 * 方案：AudioSource.MIC（同 MicRecorder，实测有声）+ Android 平台
 * AcousticEchoCanceler / NoiseSuppressor（挂 AudioRecord.audioSessionId）+ sendCustomAudioData 上行。
 * 16k/mono/20ms=640B 直采直发，无重采样；AEC 由平台音频特效在采集侧完成。
 *
 * 生命周期：RtcClient onEnterRoom 成功 → start(cloud)；exitRoom/release → stop()。
 * mic handoff 结构不变：KWS MicRecorder 在 SIGNING 停止，本源只活在会话期，退房后由
 * onExited 恢复 KWS 采集（两源不并发）。
 */
class RealCustomAudioSource : RtcClient.CustomAudioSource {

    companion object {
        private const val TAG = "RtcCustomAudio"
        private const val FRAME_SAMPLES = RtcCustomAudioPcm.SAMPLES_PER_20MS // 320
        private const val WATCHDOG_IDLE_MS = 5_000L
    }

    private val running = AtomicBoolean(false)
    private var thread: Thread? = null

    @Volatile private var aec: AcousticEchoCanceler? = null
    @Volatile private var ns: NoiseSuppressor? = null

    @SuppressLint("MissingPermission") // RECORD_AUDIO 由服务层在进房前完成授权（同 MicRecorder 契约）
    override fun start(cloud: TRTCCloud): Boolean {
        if (!running.compareAndSet(false, true)) return true
        val minBuf = AudioRecord.getMinBufferSize(
            RtcCustomAudioPcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize failed: $minBuf")
            running.set(false)
            return false
        }
        val record = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            RtcCustomAudioPcm.SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf * 2, FRAME_SAMPLES * 2 * 4)
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord init failed")
            record.release()
            running.set(false)
            return false
        }
        // 平台 AEC/NS：回音根治核心。isAvailable=false 不阻断（真机日志留痕，降级为无 AEC 上行）。
        try {
            if (AcousticEchoCanceler.isAvailable()) {
                aec = AcousticEchoCanceler.create(record.audioSessionId)?.also {
                    it.enabled = true
                    Log.i(TAG, "AEC enabled (session=${record.audioSessionId})")
                }
            } else Log.w(TAG, "AEC not available on this device")
            if (NoiseSuppressor.isAvailable()) {
                ns = NoiseSuppressor.create(record.audioSessionId)?.also {
                    it.enabled = true
                    Log.i(TAG, "NS enabled")
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "AEC/NS attach failed: ${t.message}", t)
        }

        thread = Thread({ loop(record, cloud) }, "jax-rtc-capture").apply { start() }
        Log.i(TAG, "custom capture started (16k/mono/20ms)")
        return true
    }

    private fun loop(record: AudioRecord, cloud: TRTCCloud) {
        val pcm = ShortArray(FRAME_SAMPLES)
        var lastFrameTs = System.currentTimeMillis()
        try {
            record.startRecording()
            while (running.get()) {
                val n = record.read(pcm, 0, FRAME_SAMPLES)
                if (n > 0) {
                    lastFrameTs = System.currentTimeMillis()
                    // javap 核对 13.4.0.20477：TRTCAudioFrame 仅 data/sampleRate/channel/timestamp/extraData
                    //（无 audioFormat/length，那是 Electron d.ts 的契约）
                    val frame = TRTCCloudDef.TRTCAudioFrame()
                    frame.data = RtcCustomAudioPcm.shortToPcm16le(pcm.copyOf(n))
                    frame.sampleRate = RtcCustomAudioPcm.SAMPLE_RATE
                    frame.channel = 1
                    frame.timestamp = lastFrameTs
                    try {
                        cloud.sendCustomAudioData(frame)
                    } catch (e: Exception) {
                        Log.w(TAG, "sendCustomAudioData failed: ${e.message}")
                    }
                } else if (n < 0) {
                    Log.w(TAG, "record.read=$n")
                    Thread.sleep(20)
                }
                if (System.currentTimeMillis() - lastFrameTs > WATCHDOG_IDLE_MS) {
                    Log.e(TAG, "watchdog: no frame for ${WATCHDOG_IDLE_MS}ms")
                    break
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "capture loop error", e)
        } finally {
            try { record.stop() } catch (_: Exception) {}
            try { record.release() } catch (_: Exception) {}
            Log.i(TAG, "custom capture stopped")
        }
    }

    override fun stop() {
        running.set(false)
        thread?.interrupt()
        try { thread?.join(2_000) } catch (_: InterruptedException) { Thread.currentThread().interrupt() }
        thread = null
        try { aec?.enabled = false; aec?.release() } catch (_: Throwable) {}
        try { ns?.enabled = false; ns?.release() } catch (_: Throwable) {}
        aec = null; ns = null
    }
}
