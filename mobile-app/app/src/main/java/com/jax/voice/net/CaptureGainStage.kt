package com.jax.voice.net

import kotlin.math.sqrt

/**
 * 采集增益级（2026-09-05 真机校准 v2，替代固定 ×32）：
 *
 * 三段实测证据：
 *  1. 直采 MIC 电平过低（说话 rms≈79，MUSIC 档 4000+）→ 千问 VAD 不触发 → 180s 无响应被踢；
 *  2. 固定 ×32 又过头（说话 rms 12346~19563，逼近 32000 限幅 → 削波失真、听不清）；
 *  3. 底噪被同步放大（1 → 6~30）→ 千问把环境声当对话，即「没喊波斯猫也回答」。
 *
 * 因此固定倍数不可能同时满足「轻声听得见 / 大声不削波 / 静音不误触发」，改为自适应增益 + 噪声门。
 *
 * ---------------------------------------------------------------------------
 * v2 修订（真机 12 条 lvl 样本逼出来的两个缺陷，均为结构性 bug 而非参数偏差）：
 *
 *  D1 增益噪声上冲（runaway）—— 根因：语音判定用「增益后」电平，而 desired = targetRms/rawRms。
 *     静音时 raw≈6，只要 gain 已升到 28 就有 projected≈170~280 越过门限 200 → 该帧被判为语音 →
 *     desired = 3000/6 = 500 → 被 MAX_GAIN 截断为 32 → 增益继续上冲 → 越冲越过门限。正反馈。
 *     真机实测样本：raw=6 → raw=1 → raw=0 期间增益却从 21.1 一路涨到 30.1，正是这条回路。
 *     后果：真正说话时增益停在 24~28，raw=210 直接被放大到 out=5074，raw=459 放大到 out=9702
 *     （峰值早已顶到 32000 限幅削波），等于把固定 ×32 的毛病换个形式复现。
 *     修法：① 自适应噪声底 noiseFloor + 只有明显高于底噪的帧才参与收敛；
 *           ② 高增益把底噪顶过门限时，主动向下退增益，切断正反馈。
 *
 *  D2 下调过慢 —— 原 SMOOTHING=0.05 双向对称，从 32 收敛到 14 需 ~1.2s，这 1.2s 内大声持续削波。
 *     修法：非对称收敛——上调慢（防喘息/泵音），下调快（防削波）。
 *
 * 纯 JVM 可测（无 Android 依赖）：契约见 CaptureGainStageTest。
 */
class CaptureGainStage(
    private val targetRms: Float = TARGET_RMS,
    private val minGain: Float = MIN_GAIN,
    private val maxGain: Float = MAX_GAIN,
    private val gateRms: Float = GATE_RMS,
    private val holdFrames: Int = HOLD_FRAMES,
    private val clamp: Int = CLAMP,
) {

    /** 当前增益（只读，便于日志/测试观察收敛过程） */
    var currentGain: Float = INITIAL_GAIN
        private set

    /**
     * 当前自适应噪声底（原始 rms 量纲，只读）。
     *
     * 只在非语音帧更新：否则长时间说话会把说话电平「学」成底噪，反过来把自己的语音当噪声吃掉。
     * 真机可观测性用：安静时该值应贴近环境底噪（实测 0~6），说话帧不应污染它。
     */
    var currentNoiseFloor: Float = INITIAL_NOISE_FLOOR
        private set

    private var holdRemaining = 0

    /** 最近一帧的原始 rms（真机可观测性用：判断「是没说话还是太轻」） */
    var lastRawRms: Float = 0f
        private set

    /** 最近一帧的输出 rms（真机可观测性用：验收说话应落 2000~5000） */
    var lastOutRms: Float = 0f
        private set

    /** 最近一帧门是否放行（真机可观测性用：安静时应为 false） */
    var lastGateOpen: Boolean = false
        private set

    /** 最近一帧是否参与了增益收敛（真机区分「在收敛」与「被门/底噪挡住」） */
    var lastAdapted: Boolean = false
        private set

    /**
     * 处理一帧（20ms/16k/mono 的 PCM16 采样）。返回新数组，不修改入参。
     */
    fun process(frame: ShortArray): ShortArray {
        val rawRms = rms(frame)
        lastRawRms = rawRms
        lastAdapted = false
        val gained = ShortArray(frame.size)
        if (frame.isEmpty()) {
            lastOutRms = 0f
            lastGateOpen = false
            return gained
        }

        // 语音判定用「增益后」电平，与门限同一坐标系
        val projected = rawRms * currentGain
        val isSpeech = projected >= gateRms

        if (!isSpeech) {
            // 只在非语音帧更新噪声底：说话时冻结，避免把语音学成底噪（见 currentNoiseFloor 注释）
            currentNoiseFloor = (currentNoiseFloor + (rawRms - currentNoiseFloor) * FLOOR_ADAPT)
                .coerceIn(FLOOR_MIN, FLOOR_MAX)
        }

        // 参与增益收敛的门槛：必须明显高于当前噪声底。raw=6 的底噪即使被 28× 增益顶过门限，
        // 也不得驱动增益——那正是 D1 的正反馈入口。
        val adaptFloor = (currentNoiseFloor * NOISE_MARGIN).coerceIn(RAW_FLOOR_ABS, ADAPT_FLOOR_MAX)
        val isCandidate = rawRms >= adaptFloor

        if (isSpeech && isCandidate) {
            // 真实语音：向目标电平收敛。下调快（防削波）、上调慢（防泵音）。
            val desired = (targetRms / rawRms).coerceIn(minGain, maxGain)
            val rate = if (desired < currentGain) ATTACK_DOWN else ATTACK_UP
            currentGain += (desired - currentGain) * rate
            currentGain = currentGain.coerceIn(minGain, maxGain)
            holdRemaining = holdFrames
            lastAdapted = true
        } else if (isSpeech) {
            // 这一帧「像语音」只是因为我们自己的增益把底噪放大了。向下退增益，直到投影跌回门限以内，
            // 切断「增益越高 → 越像语音 → 增益越高」的正反馈。不刷新保持窗，让门尽快关掉。
            val ceiling = (gateRms * GATE_BACKOFF / rawRms).coerceIn(minGain, maxGain)
            if (ceiling < currentGain) {
                currentGain += (ceiling - currentGain) * ATTACK_DOWN
                currentGain = currentGain.coerceIn(minGain, maxGain)
                lastAdapted = true
            }
        } else if (holdRemaining > 0) {
            // 保持窗：词间停顿/辅音起始放行，不收敛增益（静音帧不参与自适应）
            holdRemaining -= 1
        } else {
            // 全零：底噪静音，避免千问把环境声当对话
            lastOutRms = 0f
            lastGateOpen = false
            return gained
        }
        lastGateOpen = true

        val gain = currentGain
        var acc = 0.0
        for (i in frame.indices) {
            val v = (frame[i] * gain).toInt()
            val c = when {
                v > clamp -> clamp.toShort()
                v < -clamp -> (-clamp).toShort()
                else -> v.toShort()
            }
            gained[i] = c
            acc += c.toDouble() * c.toDouble()
        }
        lastOutRms = sqrt(acc / frame.size).toFloat()
        return gained
    }

    private fun rms(frame: ShortArray): Float {
        if (frame.isEmpty()) return 0f
        var acc = 0.0
        for (s in frame) {
            val v = s.toDouble()
            acc += v * v
        }
        return sqrt(acc / frame.size).toFloat()
    }

    companion object {
        /** 目标说话电平（rms）：对齐 MUSIC 档实测 4000 量级，取 3000 留削波余量 */
        const val TARGET_RMS = 3000f
        /**
         * 增益下界 = 1（单位增益，即不放大也不衰减）。
         *
         * 不能设 >1：设 2 时原始 rms 3000 的大声会被强制放大到 6000（约为限幅顶 32000 的 19%，
         * 峰值早已削顶），正是固定 ×32 时代「大声听不清」的翻版。抗噪由 GATE_RMS 噪声门负责，
         * 不靠抬高增益下界——两者职责必须分开，否则会为了压噪反而制造削波。
         */
        const val MIN_GAIN = 1f
        /** 增益上界：直采最弱语音（rms≈79）放大到 ~2500 所需，再高只会放大底噪 */
        const val MAX_GAIN = 32f
        /** 初始增益：会话起始取中值，避免首帧过冲 */
        const val INITIAL_GAIN = 8f
        /** 门限（增益后 rms）：环境 ~2~32，说话 ~2500~5000，200 可干净分开 */
        const val GATE_RMS = 200f
        /** 保持窗（帧）：20ms/帧 × 15 = 300ms */
        const val HOLD_FRAMES = 15
        /** 软限幅，留一点余量不触顶 32767 */
        const val CLAMP = 32000

        // ---- 增益收敛速率：非对称 ----
        /** 上调速率（慢）：20ms/帧 → 时间常数约 400ms，避免喘息/泵音 */
        const val ATTACK_UP = 0.05f
        /** 下调速率（快）：20ms/帧 → 时间常数约 80ms，大声时 300ms 内退出削波区 */
        const val ATTACK_DOWN = 0.25f

        // ---- 自适应噪声底（治 D1 正反馈）----
        /** 噪声底初值：真机安静环境原始 rms 实测 0~6，给 5 留余量且不会误伤 rms≈79 的轻声 */
        const val INITIAL_NOISE_FLOOR = 5f
        /** 噪声底跟踪速率：20ms/帧 → 时间常数约 1s（只在非语音帧更新） */
        const val FLOOR_ADAPT = 0.02f
        const val FLOOR_MIN = 0.5f
        const val FLOOR_MAX = 2000f
        /** 语音候选倍率：原始 rms 需达到噪声底的 3 倍才参与收敛 */
        const val NOISE_MARGIN = 3f
        /** 候选门槛绝对下界：环境极静（底噪≈0）时兜底，防止把 0 底噪放大成无限灵敏 */
        const val RAW_FLOOR_ABS = 20f
        /** 候选门槛上界：底噪极高（嘈杂环境）时兜底，避免门槛反过来吞掉正常语音 */
        const val ADAPT_FLOOR_MAX = 300f
        /**
         * 退增益目标系数：判定为「自己的增益把底噪放大成伪语音」时，把投影压到门限的这个比例，
         * 取 0.5 留一倍余量，保证下一帧稳定跌回门限以内。
         */
        const val GATE_BACKOFF = 0.5f
    }
}
