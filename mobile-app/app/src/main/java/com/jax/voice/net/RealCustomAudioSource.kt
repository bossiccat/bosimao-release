package com.jax.voice.net

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudDef
import java.util.Locale
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
 *
 * 进程内单例（2026-09-06 线程泄漏根治）：TRTCCloud 本身是进程级单例，采集源与它同生命周期。
 * 此前每个 RtcClient 各持一个实例，`running.compareAndSet` 只是实例内幂等，跨实例完全失效
 * ——真机实测并存 5 条 jax-rtc-capture 同时向同一个 TRTCCloud sendCustomAudioData（上行叠加抢麦）。
 * 单例后 start() 先 stop() 再起线程，即使上游漏调 stop 也不会泄漏。
 */
class RealCustomAudioSource private constructor() : RtcClient.CustomAudioSource {

    companion object {
        private const val TAG = "RtcCustomAudio"
        private const val FRAME_SAMPLES = RtcCustomAudioPcm.SAMPLES_PER_20MS // 320
        private const val WATCHDOG_IDLE_MS = 5_000L
        private const val CAPTURE_THREAD_NAME = "jax-rtc-capture"

        @Volatile private var INSTANCE: RealCustomAudioSource? = null

        /** 进程内唯一实例：所有 RtcClient 共用，杜绝多路采集并存 */
        @Synchronized
        fun get(): RealCustomAudioSource = INSTANCE ?: RealCustomAudioSource().also { INSTANCE = it }

        /** 真机取证：直接观测「5 路是否降为 1 路」 */
        private fun captureThreadCount(): Int =
            Thread.getAllStackTraces().keys.count { it.name == CAPTURE_THREAD_NAME }
        /**
         * 电平日志周期（帧）：20ms/帧 × 25 = 500ms。
         *
         * 原为 100 帧（2s）——真机取证时发现 2s 采样间隔粗到看不见收敛过程：增益从 32 掉到 14
         * 只要 ~300ms，2s 后早已收敛完毕，样本里只看到「增益怎么一会 21 一会 30」的噪声，
         * 无法判断是没收敛还是被底噪顶上去了。500ms 既能看清收敛，又远达不到「日志风暴」量级。
         */
        private const val LEVEL_LOG_FRAMES = 25L

    }

    private val running = AtomicBoolean(false)
    private var thread: Thread? = null

    /**
     * 采集增益级：自适应增益 + 噪声门，替代固定 ×32。
     * 详见 CaptureGainStage 文档与真机三段实测（过低/削波/底噪误触发）。
     * 单例下每次 start 重建：新会话 = 新环境，噪声底不该沿用上一会话。
     */
    private var gainStage = CaptureGainStage()

    @Volatile private var aec: AcousticEchoCanceler? = null
    @Volatile private var agc: AutomaticGainControl? = null
    @Volatile private var ns: NoiseSuppressor? = null

    @SuppressLint("MissingPermission") // RECORD_AUDIO 由服务层在进房前完成授权（同 MicRecorder 契约）
    @Synchronized
    override fun start(cloud: TRTCCloud): Boolean {
        // 单例语义：上一次会话若遗留了采集线程（上游漏调 stop / 异常退房），这里先停干净再起新线程。
        // 否则 N 条 jax-rtc-capture 会并存并同时向同一个 TRTCCloud 送音频（上行叠加抢麦）。
        if (running.get() || thread != null) {
            Log.w(TAG, "start while running (inst=${instId()}) — stopping previous capture first")
            stop()
        }
        gainStage = CaptureGainStage() // 新会话重置噪声底与增益，不沿用上一会话的收敛结果
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
        // 平台 AEC/NS/AGC：回音根治核心。isAvailable=false 不阻断（真机日志留痕，降级为无特效上行）。
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
            if (AutomaticGainControl.isAvailable()) {
                agc = AutomaticGainControl.create(record.audioSessionId)?.also {
                    it.enabled = true
                    Log.i(TAG, "AGC enabled")
                }
            } else Log.w(TAG, "AGC not available on this device")
        } catch (t: Throwable) {
            Log.w(TAG, "AEC/NS/AGC attach failed: ${t.message}", t)
        }

        thread = Thread({ loop(record, cloud) }, CAPTURE_THREAD_NAME).apply { start() }
        Log.i(
            TAG,
            "custom capture started (16k/mono/20ms) inst=${instId()} captureThreads=${captureThreadCount()}"
        )
        return true
    }

    private fun instId(): String = Integer.toHexString(System.identityHashCode(this))

    private fun loop(record: AudioRecord, cloud: TRTCCloud) {
        val pcm = ShortArray(FRAME_SAMPLES)
        var lastFrameTs = System.currentTimeMillis()
        var frameSeq = 0L
        try {
            record.startRecording()
            while (running.get()) {
                val n = record.read(pcm, 0, FRAME_SAMPLES)
                if (n > 0) {
                    lastFrameTs = System.currentTimeMillis()
                    // 自适应增益 + 噪声门（2026-09-05：固定 ×32 削波且放大底噪致误唤醒）
                    val gained = gainStage.process(pcm.copyOf(n))
                    // 每 500ms（25 帧 × 20ms）落一条电平日志：真机验收「说话 2000~5000 / 安静静音」
                    // 的唯一客观依据，缺了它只能凭「听起来行不行」猜。
                    // floor= 噪声底 / adp= 本帧是否参与收敛 —— 用来区分「在收敛」与「被底噪门槛挡住」，
                    // 没有这两个字段就无法在真机上区分 D1 runaway 与正常收敛。
                    if (++frameSeq % LEVEL_LOG_FRAMES == 0L) {
                        Log.i(
                            TAG,
                            "lvl raw=" + gainStage.lastRawRms.toInt() +
                                " gain=" + String.format(Locale.US, "%.1f", gainStage.currentGain) +
                                " out=" + gainStage.lastOutRms.toInt() +
                                " gate=" + gainStage.lastGateOpen +
                                " floor=" + String.format(Locale.US, "%.1f", gainStage.currentNoiseFloor) +
                                " adp=" + gainStage.lastAdapted
                        )
                    }
                    // javap 核对 13.4.0.20477：TRTCAudioFrame 仅 data/sampleRate/channel/timestamp/extraData
                    //（无 audioFormat/length，那是 Electron d.ts 的契约）
                    val frame = TRTCCloudDef.TRTCAudioFrame()
                    frame.data = RtcCustomAudioPcm.shortToPcm16le(gained)
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
            Log.i(TAG, "capture loop exited inst=${instId()} captureThreads=${captureThreadCount()}")
        }
    }

    @Synchronized
    override fun stop() {
        if (!running.get() && thread == null) {
            Log.d(TAG, "stop ignored: not running inst=${instId()}")
            return
        }
        running.set(false)
        thread?.interrupt()
        try { thread?.join(2_000) } catch (_: InterruptedException) { Thread.currentThread().interrupt() }
        thread = null
        try { aec?.enabled = false; aec?.release() } catch (_: Throwable) {}
        try { ns?.enabled = false; ns?.release() } catch (_: Throwable) {}
        try { agc?.enabled = false; agc?.release() } catch (_: Throwable) {}
        aec = null; ns = null; agc = null
        Log.i(TAG, "custom capture stopped inst=${instId()} captureThreads=${captureThreadCount()}")
    }
}
