package com.jax.voice.net

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.util.Log
import com.jax.voice.util.DiagLog
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
         * M0 A/B（decision-relocation §M0）：本源是播放段唯一活着的采集源，是回声耦合的
         * 主被测对象。采集源由 gradle resValue jax_capture_source 注入（spec §11-3 规定 MIC
         * → 平台 AEC 无回声参考 = 空操作，VC 变体实测平台 AEC 是否真生效；已知风险：TRTC
         * SPEECH 档绑 VC 源在本机曾送全零，VC 变体若 lvl raw 恒 0 即同路径静音，G0 判 M1）。
         * 本类无 Context，经 ActivityThread 反射取 app context 读资源——反射失败/资源缺失
         * 一律回退 MIC（与生产行为一致）。
         */
        fun resolveCaptureSource(): Int {
            return try {
                val at = Class.forName("android.app.ActivityThread")
                val ctx = at.getDeclaredMethod("currentApplication").invoke(null) as? android.content.Context
                    ?: return MediaRecorder.AudioSource.MIC
                val resId = ctx.resources.getIdentifier("jax_capture_source", "string", ctx.packageName)
                val name = if (resId != 0) ctx.getString(resId) else "MIC"
                Log.i(TAG, "jax_capture_source=$name")
                if (name == "VOICE_COMMUNICATION") MediaRecorder.AudioSource.VOICE_COMMUNICATION
                else MediaRecorder.AudioSource.MIC
            } catch (t: Throwable) {
                Log.w(TAG, "resolveCaptureSource fallback MIC: ${t.message}")
                MediaRecorder.AudioSource.MIC
            }
        }

        /**
         * 采集音效 A/B 开关（2026-09-16 真机排查）：由 gradle resValue `jax_capture_effects` 注入。
         * 返回 true 表示挂平台 AEC/NS/AGC（生产默认）；false 表示**不挂**（对照变体）。
         * 反射失败/资源缺失一律回退 true —— 与生产行为一致，绝不因读不到就悄悄改变产品行为。
         */
        fun resolveCaptureEffectsEnabled(): Boolean {
            return try {
                val at = Class.forName("android.app.ActivityThread")
                val ctx = at.getDeclaredMethod("currentApplication").invoke(null)
                    as? android.content.Context ?: return true
                val resId = ctx.resources.getIdentifier("jax_capture_effects", "string", ctx.packageName)
                val name = if (resId != 0) ctx.getString(resId) else "AEC_NS"
                Log.i(TAG, "jax_capture_effects=$name")
                name.trim().uppercase() != "NONE"
            } catch (t: Throwable) {
                Log.w(TAG, "resolveCaptureEffects fallback AEC_NS: ${t.message}")
                true
            }
        }

        /**
         * 采集音频模式 A/B（2026-09-16 根因彻查）。
         *
         * 根因：自采 AudioRecord 是在 `enterRoom(TRTC_APP_SCENE_AUDIOCALL)` **之后**创建的，
         * 此时设备处于通话音频形态（`MODE_IN_COMMUNICATION`；HAL 侧 aec/ns、输入 2ch），
         * 而本机（Samsung S26U / AGM-LPI 路径）在该形态下**向应用自采返回全零**
         * （同机同麦的 MicRecorder 走 `dev=1ch` 无音效形态 ⇒ 电平 39~72 正常）。
         *
         * 本开关只影响**建 AudioRecord 那一瞬**：NORMAL = 临时置 MODE_NORMAL 并在建完后恢复；
         * KEEP = 什么都不做（**生产默认**）。读不到资源一律回退 KEEP。
         */
        fun resolveCaptureModeNormal(): Boolean {
            return try {
                val at = Class.forName("android.app.ActivityThread")
                val ctx = at.getDeclaredMethod("currentApplication").invoke(null)
                    as? android.content.Context ?: return false
                val resId = ctx.resources.getIdentifier("jax_capture_mode", "string", ctx.packageName)
                val name = if (resId != 0) ctx.getString(resId) else "KEEP"
                Log.i(TAG, "jax_capture_mode=$name")
                name.trim().uppercase() == "NORMAL"
            } catch (t: Throwable) {
                Log.w(TAG, "resolveCaptureMode fallback KEEP: ${t.message}")
                false
            }
        }

        fun sourceName(src: Int): String = when (src) {
            MediaRecorder.AudioSource.MIC -> "MIC"
            MediaRecorder.AudioSource.VOICE_COMMUNICATION -> "VOICE_COMMUNICATION"
            else -> "src=$src"
        }
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
        // M0 A/B：每次 start 重新解析 resValue 注入的采集源（默认/异常=MIC 与生产一致）
        val captureSource = resolveCaptureSource()
        // 2026-09-16 A/B：仅在建 AudioRecord 这一瞬临时切 MODE_NORMAL（默认 KEEP 不动）
        val forceNormal = resolveCaptureModeNormal()
        val am = if (forceNormal) {
            runCatching {
                (Class.forName("android.app.ActivityThread")
                    .getDeclaredMethod("currentApplication").invoke(null)
                    as android.content.Context)
                    .getSystemService(android.content.Context.AUDIO_SERVICE)
                    as? android.media.AudioManager
            }.getOrNull()
        } else null
        val prevMode = am?.mode
        if (forceNormal && am != null) {
            runCatching { am.mode = android.media.AudioManager.MODE_NORMAL }
            Log.w(TAG, "capture mode forced NORMAL (was=$prevMode) for AudioRecord creation")
        }
        val record = AudioRecord(
            captureSource,
            RtcCustomAudioPcm.SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf * 2, FRAME_SAMPLES * 2 * 4)
        )
        if (forceNormal && am != null && prevMode != null) {
            runCatching { am.mode = prevMode }
            Log.i(TAG, "capture mode restored to $prevMode")
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord init failed")
            record.release()
            running.set(false)
            return false
        }
        // 平台 AEC/NS/AGC：回音根治核心。isAvailable=false 不阻断（真机日志留痕，降级为无特效上行）。
        // M0 G0 关键判据：enabled 回读 + created 状态留痕——若 VC 变体下 AEC created 但实测
        // 空操作（回声耦合比无改善），G0 判 M1 而非 M2。
        val effectsEnabled = resolveCaptureEffectsEnabled()
        if (!effectsEnabled) {
            Log.w(TAG, "capture effects DISABLED (jax_capture_effects=NONE) —— 对照变体，不挂 AEC/NS/AGC")
        }
        try {
            if (!effectsEnabled) {
                // 对照变体：刻意不挂任何平台音效，用于判定"采集恒零"是否由挂载造成
            } else if (AcousticEchoCanceler.isAvailable()) {
                aec = AcousticEchoCanceler.create(record.audioSessionId)?.also {
                    it.enabled = true
                    Log.i(TAG, "AEC created (session=${record.audioSessionId}) enabled=${it.enabled}")
                }
            } else Log.w(TAG, "AEC not available on this device")
            if (effectsEnabled && NoiseSuppressor.isAvailable()) {
                ns = NoiseSuppressor.create(record.audioSessionId)?.also {
                    it.enabled = true
                    Log.i(TAG, "NS created enabled=${it.enabled}")
                }
            }
            if (effectsEnabled && AutomaticGainControl.isAvailable()) {
                agc = AutomaticGainControl.create(record.audioSessionId)?.also {
                    it.enabled = true
                    Log.i(TAG, "AGC created enabled=${it.enabled}")
                }
            } else Log.w(TAG, "AGC not available on this device")
        } catch (t: Throwable) {
            Log.w(TAG, "AEC/NS/AGC attach failed: ${t.message}", t)
        }

        thread = Thread({ loop(record, cloud) }, CAPTURE_THREAD_NAME).apply { start() }
        Log.i(
            TAG,
            "custom capture started (16k/mono/20ms) source=${sourceName(captureSource)} effects=${if (effectsEnabled) "AEC_NS" else "NONE"} inst=${instId()} captureThreads=${captureThreadCount()}"
        )
        return true
    }

    private fun instId(): String = Integer.toHexString(System.identityHashCode(this))

    // 2026-09-16 修正：本看门狗初版的判据是"连续 5 秒精确零电平 ⇒ 麦克风无输入"，
    // 但当天对齐采样实测证明**安静房间里连续 15 秒精确 0 是正常的**（说话时 raw 可达 1029）
    // ⇒ 那个判据会对正常静音误报。现在只作**诊断提示**，不报错，窗口放宽到 30 秒，
    // 且措辞不再断言"麦克风无输入"（它无法区分"用户没说话"与"采集失效"）。
    private val silentWatchdog = SilentInputWatchdog(
        maxZeroFrames = 1500,                      // 20ms/帧 × 1500 = 30 秒
        onSilent = { frames ->
            Log.w(TAG, "no non-zero input for $frames frames (30s); user may simply be silent")
        },
    )

    private fun loop(record: AudioRecord, cloud: TRTCCloud) {
        val pcm = ShortArray(FRAME_SAMPLES)
        var lastFrameTs = System.currentTimeMillis()
        var frameSeq = 0L
        try {
            record.startRecording()
            // 2026-09-16 真机取证埋点（纯观测）：与 MicRecorder 同口径，看**进会话后**绑到哪个输入设备。
            // 对照事实：会话外那条路径实测 routedDevice=BUILTIN_MIC/addr=bottom 且 lvl raw=39~72；
            // 本条路径在同一台机器上恒 0 ⇒ 必须看清"绑错了麦"还是"TRTC 抢麦"。
            runCatching {
                val rd = record.routedDevice
                // AudioDeviceInfo#getAddress 是 API 28 才有的 API，而本模块 minSdk=26。
                // 不做版本守卫的话，API 26/27 上这一行会抛，整条日志被 runCatching 吞掉
                // ⇒ 旧设备上"绑到哪个麦"的诊断信息静默消失（不是 lint 洁癖，2026-09-19 CI [NewApi] 实测暴露）。
                val addr = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) rd?.address else null
                Log.i(TAG, "routedDevice type=${rd?.type} id=${rd?.id} product=${rd?.productName} addr=$addr")
            }.onFailure { Log.w(TAG, "routedDevice read failed: ${it.message}") }
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
                    // C4 fail-loud（2026-09-16）：连续 5 秒精确零电平 ⇒ 主动报错。
                    // 本轮真机事故：上行恒零时应用照旧宣称 IN_ROOM、不报任何错，
                    // 用户只能靠"说话没反应"发现。看门狗把静音变成显式事件（DiagLog 可导出）。
                    if (silentWatchdog.feed(gainStage.lastRawRms)) {
                        // 诊断提示，**不是**错误：安静 30 秒是正常使用场景。
                        // 真正确认"采集失效"需要一个我们目前没有的独立判据（例如与系统
                        // 录音路径同时刻对照），所以这里不声称麦克风故障，只留一条可导出的痕迹。
                        Log.w(TAG, "30s 无任何非零输入（用户可能没说话）")
                        DiagLog.log(TAG, "capture: 30s without any non-zero input (diagnostic only)")
                    }
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
