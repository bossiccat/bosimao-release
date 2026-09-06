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
 *  5. 输出永不越界（软限幅）；
 *  6. 底噪不得驱动增益上冲（D1 runaway 回归）；
 *  7. 高增益后遇大声必须快速退出削波区（D2 下调过慢回归）；
 *  8. 噪声底只在非语音帧更新（说话不得把自己学成底噪）。
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

    /**
     * D1 回归：真机 12 条 lvl 样本实测，静音段（raw=6→1→0）增益却从 21.1 涨到 30.1。
     *
     * 成因：语音判定用增益后电平，而 desired = targetRms/rawRms —— 底噪 raw=6 在 gain=28 时
     * projected≈170~280 越过门限 200，于是被当语音，desired=500 被 MAX_GAIN 截断为 32，
     * 增益继续上冲 → 越冲越过门限，形成正反馈。
     *
     * 本例先把增益喂高（轻声段），再喂底噪，断言增益必须回落且门最终关闭。
     */
    @Test
    fun `ambient noise pulls gain back down instead of driving it up`() {
        val stage = CaptureGainStage()
        repeat(120) { stage.process(frameWithRms(79f)) }  // 轻声段：把增益推到接近上限
        val gainAfterSpeech = stage.currentGain
        assertTrue("前置条件：轻声段应把增益推高，实际 $gainAfterSpeech", gainAfterSpeech > 25f)

        // 真机底噪量级 raw≈6~8：旧实现会在此把增益一路推到 MAX_GAIN 且门常开
        var out = ShortArray(320)
        repeat(150) { out = stage.process(frameWithRms(8f)) } // 3s

        assertTrue(
            "底噪不得把增益顶到上限，实际 ${stage.currentGain}",
            stage.currentGain < gainAfterSpeech
        )
        assertEquals("底噪段门必须关闭，否则等于把放大后的环境声送给千问", false, stage.lastGateOpen)
        assertEquals("底噪段输出必须静音", 0f, rmsOf(out), 0f)
    }

    /**
     * D2 回归：原 SMOOTHING=0.05 双向对称，从 32 收敛到 14 需 ~1.2s，这 1.2s 内大声持续削波。
     * 真机样本 raw=459 / gain=21.1 / out=9702（峰值已顶 32000 限幅）正是这段未收敛区。
     */
    @Test
    fun `loud speech exits the clipping zone within 500ms after a high-gain state`() {
        val stage = CaptureGainStage()
        repeat(150) { stage.process(frameWithRms(79f)) }  // 把增益推到 ~32（模拟 D1 后的残留高增益）
        val gainBefore = stage.currentGain
        assertTrue("前置条件：增益应已接近上限，实际 $gainBefore", gainBefore > 28f)

        var out = ShortArray(320)
        repeat(25) { out = stage.process(frameWithRms(3000f)) } // 500ms 大声
        val rms = rmsOf(out)

        assertTrue("大声 500ms 内输出 rms 必须回落到 5000 以内，实际 $rms", rms < 5000f)
        assertTrue(
            "大声帧不得削波到限幅顶（旧实现 25 帧仍在 ~28000）",
            out.all { it.toInt() < CaptureGainStage.CLAMP }
        )
        assertTrue("增益必须已大幅回落，实际 ${stage.currentGain}", stage.currentGain < 3f)
    }

    @Test
    fun `noise floor tracks ambient frames`() {
        val stage = CaptureGainStage()
        // raw=6、gain=8 → projected=48 < 门限 200 → 非语音帧 → 噪声底应跟随到 ~6
        repeat(400) { stage.process(frameWithRms(6f)) }
        val floor = stage.currentNoiseFloor
        assertTrue("噪声底应收敛到环境量级（实测 0~6），实际 $floor", floor > 1f && floor < 12f)
    }

    @Test
    fun `speech frames do not pollute the noise floor`() {
        val stage = CaptureGainStage()
        val initial = stage.currentNoiseFloor
        // 持续说话：全部为语音帧，噪声底必须冻结，否则会把说话电平学成底噪反过来吃掉语音
        repeat(400) { stage.process(frameWithRms(600f)) }
        val floor = stage.currentNoiseFloor
        assertTrue(
            "持续说话不得抬高噪声底（否则会把自己的语音当噪声），initial=$initial now=$floor",
            floor <= initial + 0.001f
        )
    }
}
