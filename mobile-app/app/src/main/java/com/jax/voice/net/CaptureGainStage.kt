package com.jax.voice.net

import kotlin.math.sqrt

/**
 * 采集增益级（2026-09-05 真机校准，替代固定 ×32）：
 *
 * 三段实测证据：
 *  1. 直采 MIC 电平过低（说话 rms≈79，MUSIC 档 4000+）→ 千问 VAD 不触发 → 180s 无响应被踢；
 *  2. 固定 ×32 又过头（说话 rms 12346~19563，逼近 32000 限幅 → 削波失真、听不清）；
 *  3. 底噪被同步放大（1 → 6~30）→ 千问把环境声当对话，即「没喊波斯猫也回答」。
 *
 * 因此固定倍数不可能同时满足「轻声听得见 / 大声不削波 / 静音不误触发」，改为：
 *  - 自适应增益（AGC 替代）：逐帧向 TARGET_RMS 收敛，增益限幅 [MIN_GAIN, MAX_GAIN]，
 *    只在判定为语音的帧上收敛（静音帧不参与，避免把底噪当目标把增益顶到最大）；
 *  - 噪声门：增益后 rms 低于 GATE_RMS 的帧直接输出静音（治「没喊也回答」）；
 *  - 保持窗 HOLD_FRAMES：门关闭后仍放行一段时间，避免吃掉词头与词间停顿。
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

    /**
     * 处理一帧（20ms/16k/mono 的 PCM16 采样）。返回新数组，不修改入参。
     */
    fun process(frame: ShortArray): ShortArray {
        val rawRms = rms(frame)
        lastRawRms = rawRms
        val gained = ShortArray(frame.size)
        if (frame.isEmpty()) {
            lastOutRms = 0f
            lastGateOpen = false
            return gained
        }

        // 语音判定用「增益后」电平，与门限同一坐标系
        val projected = rawRms * currentGain
        val isSpeech = projected >= gateRms

        if (isSpeech) {
            // 向目标电平收敛：desired 由本帧原始电平推出，平滑避免逐帧跳变（喘息/泵音）
            val desired = (targetRms / rawRms).coerceIn(minGain, maxGain)
            currentGain += (desired - currentGain) * SMOOTHING
            currentGain = currentGain.coerceIn(minGain, maxGain)
            holdRemaining = holdFrames
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
        /** 收敛平滑系数：20ms/帧 → 时间常数约 400ms */
        const val SMOOTHING = 0.05f
        /** 软限幅，留一点余量不触顶 32767 */
        const val CLAMP = 32000
    }
}
