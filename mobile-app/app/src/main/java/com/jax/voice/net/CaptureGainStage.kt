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
 * ---------------------------------------------------------------------------
 * v3 修订（2026-09-06 真机第二轮 240s 采样：gate=true 占 98.8%，底噪 raw 40~88 × 32 = 1280~2816
 *    远超门限 200 → 环境声被当语音持续上行 → 千问无缘无故回答 / 用户说话被抢话）：
 *
 *  D3 门限固定常量 × 增益变量必然失配 —— 旧门控判定在「增益后」域（raw*gain >= 200），而增益会
 *     跟着环境往上走。门限是常数、增益是变量，房间够吵时任何固定门限都会被增益顶穿。
 *     这是设计缺陷，不是参数没调好。
 *     修法：门控判定改到「原始域」——「是不是语音」用原始电平相对自适应噪声底判断，
 *     增益只负责把确认是语音的帧放大到目标电平，不再参与「是不是语音」的判定。
 *
 *  D4 噪声底只在非语音帧更新，嘈杂房间学不到 —— 旧实现 `if (!isSpeech) floor更新`，而嘈杂房间
 *     里门常开（isSpeech 恒真），永远等不到非语音帧，底噪根本学不到新环境。
 *     修法：全帧更新，但下降快（环境变安静立刻跟下来）/ 上升慢（~7s 感知到变吵）/
 *     上限封顶 FLOOR_CEILING（连续说话不把自己的电平学成底噪）。
 *
 *  权衡（诚实面对）：单麦克风、无回声参考信号下的物理限制——视频外放 raw 88 与轻声 184 只差
 *  2 倍多，门限卡在 ~176 意味着更轻的说话声会被吃掉。我们选择「宁可漏接轻声，也不能把视频声
 *  送进云端」，因为后者直接摧毁产品可信度。真正的解法是 AEC（需要参考信号，本机
 *  VOICE_COMMUNICATION 源送全零，是死路）或多麦波束。
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
     * v3：全帧更新（不再只在非语音帧）。下降快（FLOOR_FALL，环境变安静立刻跟下来）、
     * 上升慢（FLOOR_RISE，~7s 感知到变吵）、上限封顶 FLOOR_CEILING（连续说话不把自己的电平
     * 学成底噪）。真机可观测性用：安静时该值应贴近环境底噪，视频外放时应爬到环境量级。
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

        // 语音判定（v3）：「是不是语音」用原始电平相对自适应噪声底判断，增益后电平只作绝对下界。
        // 增益不再参与「是不是语音」的判定——门限是常数、增益是变量，两者相乘必然失配（D3）。
        currentNoiseFloor = if (rawRms < currentNoiseFloor) {
            // 下降快：环境变安静要立刻跟下来，否则静音期误放行
            currentNoiseFloor + (rawRms - currentNoiseFloor) * FLOOR_FALL
        } else {
            // 上升慢但可感知：环境变吵要在 ~7s 内跟上去，否则门限永远学不到新环境；
            // 上限封顶：防止连续说话把自己的电平学成底噪
            minOf(currentNoiseFloor + (rawRms - currentNoiseFloor) * FLOOR_RISE, FLOOR_CEILING)
        }
        currentNoiseFloor = currentNoiseFloor.coerceIn(FLOOR_MIN, FLOOR_CEILING)

        // 原始域候选门槛：明显高于噪声底才算语音。视频外放 raw 40~88 在底噪学上去后被整体门掉，
        // 语音 raw 184~459 仍通过（D3/D4 核心）。
        val adaptFloor = (currentNoiseFloor * NOISE_MARGIN).coerceIn(RAW_GATE_ABS, ADAPT_FLOOR_MAX)
        val isCandidate = rawRms >= adaptFloor
        val projected = rawRms * currentGain
        // 双条件：原始域候选 + 增益后绝对下界。projected 低于门限的帧对云端 VAD 本就无意义。
        val isSpeech = isCandidate && projected >= gateRms

        if (isSpeech) {
            // 真实语音：向目标电平收敛。下调快（防削波）、上调慢（防泵音）。
            val desired = (targetRms / rawRms).coerceIn(minGain, maxGain)
            val rate = if (desired < currentGain) ATTACK_DOWN else ATTACK_UP
            currentGain += (desired - currentGain) * rate
            currentGain = currentGain.coerceIn(minGain, maxGain)
            holdRemaining = holdFrames
            lastAdapted = true
        } else if (projected >= gateRms) {
            // 伪语音：projected 过了门限但原始域不是候选——只可能是我们自己的增益把底噪放大了。
            // 向下退增益，直到投影跌回门限以内，切断「增益越高 → 越像语音 → 增益越高」的正反馈
            // （D1 保护保留，判定域改为原始域候选）。不刷新保持窗，让门尽快关掉。
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

        // ---- 自适应噪声底（v3：全帧更新，治 D1 正反馈 + D3/D4 嘈杂房间门常开）----
        /** 噪声底初值：真机安静环境原始 rms 实测 0~6，给 5 留余量且不会误伤 rms≈79 的轻声 */
        const val INITIAL_NOISE_FLOOR = 5f
        /** 噪声底下限：环境极静（底噪≈0）时兜底 */
        const val FLOOR_MIN = 0.5f
        /**
         * 噪声底上限（v3，原 FLOOR_MAX=2000）：封顶防止连续说话把自己的电平学成底噪。
         * 取 120：视频外放实测底噪 raw 40~88 < 120 可学上去，而连续大声说话（raw 600）
         * 被封在 120 → 候选门槛 240 < 600 语音仍通过。
         */
        const val FLOOR_CEILING = 120f
        /** 噪声底下降速率：20ms/帧 → 时间常数约 100ms，环境变安静立刻跟下来（防静音期误放行） */
        const val FLOOR_FALL = 0.2f
        /** 噪声底上升速率：20ms/帧 → 时间常数约 6.7s，环境变吵 ~7s 内学上去（防门限学不到新环境） */
        const val FLOOR_RISE = 0.003f
        /** 语音候选倍率（v3，原 3f）：原始 rms 需达到噪声底的 2 倍才算语音。底噪封顶后裕度需更紧 */
        const val NOISE_MARGIN = 2f
        /**
         * 候选门槛绝对下界（v3，原 RAW_FLOOR_ABS=20f）：环境极静时兜底。
         * 取 60：raw < 60 的帧对云端 VAD 本来就无意义，放行只会送纯噪声。
         */
        const val RAW_GATE_ABS = 60f
        /** 候选门槛上界：底噪极高（嘈杂环境）时兜底，避免门槛反过来吞掉正常语音 */
        const val ADAPT_FLOOR_MAX = 300f
        /**
         * 退增益目标系数：判定为「自己的增益把底噪放大成伪语音」时，把投影压到门限的这个比例，
         * 取 0.5 留一倍余量，保证下一帧稳定跌回门限以内。
         */
        const val GATE_BACKOFF = 0.5f
    }
}
