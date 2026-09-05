package com.jax.voice.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.sqrt

/**
 * 采集增益级契约（2026-09-05 真机校准驱动）：
 *  1. 底噪帧 → 输出静音（治「没喊波斯猫也回答」：底噪 1×32=32 被千问当语音）；
 *  2. 轻声（rms≈79）→ 增益上扬，多帧后进入 2000~5000 目标区间；
 *  3. 大声（rms≈3000）→ 增益回落，输出不削波；
 *  4. 保持窗内静音放行（不吃词头/词间停顿），超出保持窗才静音；
 *  5. 输出永不越界（软限幅）。
 */
class CaptureGainStageTest {

    /** 生成一帧近似目标 rms 的信号（16k/20ms = 320 采样） */
    private fun frameWithRms(targetRms: Float, samples: Int = 320): ShortArray {
        val out = ShortArray(samples)
        if (targetRms <= 0f) return out
        // 方波近似：|v| = rms，符号交替（避免依赖正弦相位）
        for (i in out.indices) {
            val sign = if (i % 2 == 0) 1 else -1
            out[i] = (targetRms * sign).toInt().coerceIn(-32768, 32767).toShort()
        }
        return out
    }

    private fun rmsOf(frame: ShortArray): Float {
        if (frame.isEmpty()) return 0f
        var acc = 0.0
        for (s in frame) acc += s.toDouble() * s.toDouble()
        return sqrt(acc / frame.size).toFloat()
    }

    @Test
    fun `ambient noise below gate is muted`() {
        val stage = CaptureGainStage()
        // 真机底噪原始 rms≈1，即使按最大增益 32 也只有 32 << 门限 200
        val out = stage.process(frameWithRms(1f))
        assertEquals("底噪帧必须输出静音", 0f, rmsOf(out), 0f)
        assertTrue(out.all { it == 0.toShort() })
        // 可观测性字段必须自洽：真机靠 raw/out/gate 三元组判断「没说话」还是「太轻被门吃掉」
        assertEquals("原始电平应如实上报", 1f, stage.lastRawRms, 0.5f)
        assertEquals("静音帧输出电平应为 0", 0f, stage.lastOutRms, 0f)
        assertEquals("底噪帧门应关闭", false, stage.lastGateOpen)
    }

    @Test
    fun `speech frame reports gate open and gained level`() {
        val stage = CaptureGainStage()
        repeat(60) { stage.process(frameWithRms(79f)) }
        assertEquals("语音帧门必须放行", true, stage.lastGateOpen)
        assertTrue("输出电平应如实上报（验收说话落 2000~5000）", stage.lastOutRms > 2000f)
    }

    @Test
    fun `quiet speech converges into target band`() {
        val stage = CaptureGainStage()
        var last = ShortArray(320)
        repeat(60) { last = stage.process(frameWithRms(79f)) } // 1.2s 收敛
        val rms = rmsOf(last)
        assertTrue("轻声说话输出 rms 应进入 2000~5000，实际 $rms", rms > 2000f && rms < 5000f)
    }

    @Test
    fun `loud speech is attenuated and never clipped`() {
        val stage = CaptureGainStage()
        var last = ShortArray(320)
        repeat(60) { last = stage.process(frameWithRms(3000f)) }
        val rms = rmsOf(last)
        assertTrue("大声说话输出 rms 应回落到 5000 以内，实际 $rms", rms < 5000f)
        assertTrue("不得削波到限幅顶", last.all { it.toInt() < CaptureGainStage.CLAMP })
    }

    @Test
    fun `hold window preserves inter-word gaps`() {
        val stage = CaptureGainStage()
        repeat(20) { stage.process(frameWithRms(600f)) } // 说话
        val inHold = stage.process(frameWithRms(1f))     // 紧接着的静音（保持窗内）
        assertTrue("保持窗内的静音应放行（不吃词间停顿）", inHold.any { it != 0.toShort() })
        // 超出保持窗后再静音
        repeat(CaptureGainStage.HOLD_FRAMES + 2) { stage.process(frameWithRms(1f)) }
        val afterHold = stage.process(frameWithRms(1f))
        assertTrue("超出保持窗应静音", afterHold.all { it == 0.toShort() })
    }

    @Test
    fun `output never exceeds clamp`() {
        val stage = CaptureGainStage()
        val hot = ShortArray(320) { 30000 }
        val out = stage.process(hot)
        assertTrue(out.all { it.toInt() <= CaptureGainStage.CLAMP && it.toInt() >= -CaptureGainStage.CLAMP })
    }

    @Test
    fun `gain stays within bounds`() {
        val stage = CaptureGainStage()
        repeat(200) { stage.process(frameWithRms(1f)) }   // 只喂底噪：不得因静音把增益顶到最大
        assertTrue("静音帧不得驱动增益", stage.currentGain <= CaptureGainStage.INITIAL_GAIN + 1f)
        repeat(200) { stage.process(frameWithRms(79f)) }
        assertTrue(stage.currentGain <= CaptureGainStage.MAX_GAIN + 0.001f)
        assertTrue(stage.currentGain >= CaptureGainStage.MIN_GAIN - 0.001f)
    }
}
