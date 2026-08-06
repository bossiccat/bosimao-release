package com.jax.voice.voice

import android.content.res.AssetManager
import android.util.Log
import com.jax.voice.config.VoiceConfig
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit

/**
 * 唤醒词引擎 — sherpa-onnx KeywordSpotter（spec §4.2 主选）。
 *
 * 模型：sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01（Apache-2.0，中文免训练）
 * 建模单元 = 拼音（声母+韵母），模型 README 声明结构为 **zipformer**（非 zipformer2）；
 * 关键词必须以拼音音素串传给 createStream（`b ō s ī m āo @波斯猫`，见 VoiceConfig.WAKEWORD_PINYIN）。
 *
 * 线程模型（消除 native 跨线程竞态，根因修复）：
 * - createStream/process/decode/release **全部收敛到单一专用线程 `jax-kws`**；
 * - 模型加载（KeywordSpotter 构造，较重）也在该后台线程执行，不阻塞主线程（防 ANR）；
 * - 麦克风采集线程只把最新一帧投递到 kws 线程（丢弃积压帧，保证实时）；
 * - release() 把释放任务提交到同一线程执行并等待（超时 1s 兜底），绝不跨线程释放 JNI 对象。
 */
class WakeWordEngine(
    assetManager: AssetManager,
    private val threshold: Float,
    private val onWake: (String) -> Unit,
    private val onReady: (Boolean) -> Unit = {}
) {
    companion object {
        private const val TAG = "WakeWordEngine"
        private const val SAMPLE_RATE = 16000
        private const val MODEL_DIR = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        private const val RELEASE_TIMEOUT_MS = 1_000L
    }

    /** 单一 KWS 专用线程：所有 JNI 调用（create/process/decode/release）串行于此 */
    private val kwsExecutor: ExecutorService =
        Executors.newSingleThreadExecutor { r -> Thread(r, "jax-kws").apply { isDaemon = true } }

    private var spotter: KeywordSpotter? = null
    private var stream: OnlineStream? = null

    /** 模型是否就绪（异步加载完成后置 true；就绪前 process 直接丢帧） */
    @Volatile
    var isReady: Boolean = false
        private set

    /** 已释放标志：置位后 process 一律丢弃，不再投递 */
    @Volatile
    private var released = false

    /** 最新待处理帧（投递前替换 —— 积压时只处理最新，防队列膨胀） */
    @Volatile
    private var pendingFrame: FloatArray? = null

    init {
        // 模型加载 + createStream 全部移后台线程（防 ANR；与 process/release 同线程，防竞态）
        submit {
            try {
                val spot = buildSpotter(assetManager)
                val s = try {
                    spot.createStream(VoiceConfig.WAKEWORD_PINYIN)
                } catch (t: Throwable) {
                    Log.e(TAG, "createStream threw: ${t.message}")
                    null
                }
                if (s == null || s.ptr == 0L) {
                    Log.e(TAG, "createStream failed for keywords=${VoiceConfig.WAKEWORD_PINYIN} — 唤醒功能降级（不崩溃）")
                } else {
                    stream = s
                    isReady = true
                }
                spotter = spot
                Log.i(TAG, "sherpa-onnx KWS init done, threshold=$threshold, keywords=${VoiceConfig.WAKEWORD}, streamOk=${isReady}")
                onReady(isReady)
            } catch (t: Throwable) {
                // 模型加载异常：降级（不崩进程），唤醒功能不可用但 App 照常运行
                Log.e(TAG, "KWS init failed (degraded): ${t.message}", t)
                isReady = false
                onReady(false)
            }
        }
    }

    /** 在 kws 线程上构造模型（重 IO，绝不占主线程）。v0.4.4：改用 int8 模型（11MB→4MB，移动端标准，加载更快更稳） */
    private fun buildSpotter(assetManager: AssetManager): KeywordSpotter {
        val modelConfig = OnlineModelConfig(
            transducer = OnlineTransducerModelConfig(
                encoder = "$MODEL_DIR/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                decoder = "$MODEL_DIR/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                joiner = "$MODEL_DIR/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
            ),
            tokens = "$MODEL_DIR/tokens.txt",
            numThreads = 2,
            provider = "cpu",
            debug = false,
            // 模型 README 声明为 zipformer（非 zipformer2）；错配会导致模型加载失败/行为异常
            modelType = "zipformer"
        )
        val config = KeywordSpotterConfig(
            featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80),
            modelConfig = modelConfig,
            keywordsFile = "",
            keywordsThreshold = threshold
        )
        return KeywordSpotter(assetManager = assetManager, config = config)
    }

    /** 喂一帧 16k 单声道 Float PCM（-1..1）。采集线程调用：仅投递，不碰 JNI。 */
    fun process(samples: FloatArray) {
        if (released || !isReady) return
        // 替换式投递：KWS 处理慢于采集时只处理最新一帧（实时性优先，丢帧可接受）
        pendingFrame = samples
        submit {
            val frame = pendingFrame ?: return@submit
            pendingFrame = null
            processInternal(frame)
        }
    }

    /** 实际处理（仅 kws 线程执行） */
    private fun processInternal(samples: FloatArray) {
        val s = stream ?: return
        val sp = spotter ?: return
        if (s.ptr == 0L) return
        try {
            s.acceptWaveform(samples, SAMPLE_RATE)
            while (sp.isReady(s)) {
                sp.decode(s)
                val keyword = sp.getResult(s).keyword
                if (keyword.isNotBlank()) {
                    Log.i(TAG, "Wake word detected: $keyword")
                    onWake(keyword)
                    // 命中后必须 reset 流继续监听（官方示例要求）
                    sp.reset(s)
                }
            }
        } catch (t: Throwable) {
            // 单帧 JNI 异常只记日志丢弃，不退出、不崩线程
            Log.e(TAG, "process frame failed: ${t.message}")
        }
    }

    /**
     * 释放：提交到 kws 线程执行（与 process 同线程，杜绝 use-after-free），等待完成（1s 兜底）。
     * 调用方（主线程 onDestroy）只阻塞至多 1s；进程被杀/重启场景由守护线程兜底回收。
     */
    fun release() {
        if (released) return
        released = true
        pendingFrame = null
        try {
            submit {
                try {
                    stream?.release()
                } catch (t: Throwable) {
                    Log.e(TAG, "stream release failed: ${t.message}")
                }
                stream = null
                try {
                    spotter?.release()
                } catch (t: Throwable) {
                    Log.e(TAG, "spotter release failed: ${t.message}")
                }
                spotter = null
                isReady = false
            }
            kwsExecutor.shutdown()
            if (!kwsExecutor.awaitTermination(RELEASE_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
                Log.w(TAG, "kws executor not terminated in ${RELEASE_TIMEOUT_MS}ms")
                kwsExecutor.shutdownNow()
            }
        } catch (t: Throwable) {
            Log.e(TAG, "release failed: ${t.message}")
            kwsExecutor.shutdownNow()
        }
    }

    /** 统一投递入口：executor 关闭后投递静默丢弃（不抛给调用方） */
    private fun submit(task: Runnable) {
        if (released && kwsExecutor.isShutdown) return
        try {
            kwsExecutor.execute(task)
        } catch (_: RejectedExecutionException) {
            // executor 已关闭：静默丢弃
        } catch (t: Throwable) {
            Log.e(TAG, "submit failed: ${t.message}")
        }
    }
}
