package com.jax.voice.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.SystemClock
import android.util.Log

/**
 * 常驻麦克风采集（spec §4.1）：
 * AudioRecord(MIC, 16000, MONO, PCM16)，40ms/帧（640 samples）循环读取，专用采集线程。
 *
 * 注意（spec §11-3）：必须用 AudioSource.MIC，勿用 VOICE_COMMUNICATION / VOICE_CALL
 * （会被系统 AEC/NS 处理，破坏原始 16k 流）。
 *
 * 稳定性（根因修复）：
 * - **逐帧防御**：单帧 onFrame 异常只跳过当帧并计数，绝不退出循环（防静默死）；
 * - **连续失败阈值**：连续 [MAX_CONSECUTIVE_FRAME_ERRORS] 帧失败才判定管线死亡，停止并上报；
 * - **假死看门狗**：帧间无进展超过 [WATCHDOG_IDLE_MS] 判定假死（record.read 卡死/采集停滞），停止并上报；
 * - **Error 不吞**：onFrame 抛 Error（OOM/UnsatisfiedLinkError）→ 记日志 + 停止 + 上报（由服务重建），不再裸崩进程；
 * - onDied 回调由服务设置，用于重建管线 / 更新 UI（把"静默死"变成可见状态）。
 */
class MicRecorder(private val onFrame: (FloatArray) -> Unit) {

    internal interface StopControl {
        fun release()
    }

    companion object {
        private const val TAG = "MicRecorder"
        const val SAMPLE_RATE = 16000
        private const val FRAME_MS = 40
        private const val FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000 // 640

        /** 连续帧异常达到该阈值 → 判定管线死亡（约 2s 连续异常） */
        private const val MAX_CONSECUTIVE_FRAME_ERRORS = 50

        /** 帧间无进展看门狗：40ms/帧，超过 5s 无新帧 → 判定假死 */
        private const val WATCHDOG_IDLE_MS = 5_000L
    }

    @Volatile
    private var running = false

    private var thread: Thread? = null
    private var audioRecord: AudioRecord? = null

    /** 采集循环意外死亡回调（服务用它重建管线 / 更新 UI） */
    @Volatile
    private var onDied: (() -> Unit)? = null

    fun setOnDied(callback: (() -> Unit)?) {
        onDied = callback
    }

    fun start(): Boolean {
        if (running) return true
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize failed: $minBuf")
            return false
        }
        val record = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf * 2, FRAME_SAMPLES * 2 * 2)
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord init failed")
            record.release()
            return false
        }
        audioRecord = record
        running = true
        thread = Thread({ loop(record) }, "jax-mic").apply { start() }
        return true
    }

    private fun loop(record: AudioRecord) {
        val pcm = ShortArray(FRAME_SAMPLES)
        val floatBuf = FloatArray(FRAME_SAMPLES)
        var consecutiveErrors = 0
        var lastFrameTs = SystemClock.elapsedRealtime()
        var died: Throwable? = null
        try {
            record.startRecording()
            while (running) {
                val n = record.read(pcm, 0, FRAME_SAMPLES)
                if (n > 0) {
                    lastFrameTs = SystemClock.elapsedRealtime()
                    try {
                        for (i in 0 until n) floatBuf[i] = pcm[i] / 32768.0f
                        onFrame(floatBuf.copyOf(n))
                        consecutiveErrors = 0
                    } catch (e: Error) {
                        // Error（OOM/UnsatisfiedLinkError/StackOverflow）→ 记录 + 停止（不再裸崩进程）
                        Log.e(TAG, "frame onFrame Error: ${e.message}", e)
                        died = e
                        break
                    } catch (e: Exception) {
                        // 单帧异常：跳过该帧继续采集（防静默死）；连续失败才停
                        consecutiveErrors++
                        if (consecutiveErrors >= MAX_CONSECUTIVE_FRAME_ERRORS) {
                            Log.e(TAG, "consecutive frame errors >= $MAX_CONSECUTIVE_FRAME_ERRORS, pipeline dead", e)
                            died = e
                            break
                        }
                        Log.w(TAG, "frame error #$consecutiveErrors: ${e.message}")
                    }
                } else if (n < 0) {
                    // record.read 返回负值 = 读取错误：计数，连续失败才停
                    consecutiveErrors++
                    Log.w(TAG, "record.read=$n (#$consecutiveErrors)")
                    if (consecutiveErrors >= MAX_CONSECUTIVE_FRAME_ERRORS) {
                        died = IllegalStateException("record.read=$n x $MAX_CONSECUTIVE_FRAME_ERRORS")
                        break
                    }
                    Thread.sleep(20)
                }

                // 假死看门狗：长时间无新帧（read 卡死/采集停滞）→ 判定死亡
                if (SystemClock.elapsedRealtime() - lastFrameTs > WATCHDOG_IDLE_MS) {
                    Log.e(TAG, "watchdog: no frame for ${WATCHDOG_IDLE_MS}ms, treating as dead")
                    died = IllegalStateException("watchdog timeout: no frame for ${WATCHDOG_IDLE_MS}ms")
                    break
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "record loop error", e)
            died = e
        } finally {
            running = false
            try {
                record.stop()
            } catch (_: Exception) {
            }
            try {
                record.release()
            } catch (_: Exception) {
            }
            audioRecord = null
            // 非主动 stop 且异常死亡 → 上报服务（重建管线 / 更新 UI）
            if (died != null) {
                try {
                    onDied?.invoke()
                } catch (_: Throwable) {
                    // 上报回调绝不能崩采集线程
                }
            }
        }
    }

    fun stop() {
        running = false
        val worker = thread
        val record = audioRecord
        stopAndJoin(worker, object : StopControl {
            override fun release() {
                try {
                    record?.stop()
                } catch (_: Exception) {
                }
                try {
                    record?.release()
                } catch (_: Exception) {
                }
            }
        })
        thread = null
        audioRecord = null
    }

    private fun stopAndJoin(worker: Thread?, control: StopControl) {
        control.release()
        worker?.interrupt()
        if (worker != null && worker !== Thread.currentThread()) {
            try {
                worker.join(2_000)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
    }
}
