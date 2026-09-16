package com.jax.voice.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 「上行采集静音看门狗」的行为验收（纯 JVM）。 */
class SilentInputWatchdogTest {

    private var fired = 0L
    private var lastRun = 0L
    private val watchdog = SilentInputWatchdog(
        maxZeroFrames = 250,
        onSilent = { fired++; lastRun = it },
    )

    @Test
    fun fires_once_after_the_threshold_is_reached() {
        repeat(249) { assertFalse(watchdog.feed(0f)) }
        assertTrue(watchdog.feed(0f))           // 第 250 帧触发（阈值=250）
        assertEquals(1, fired)
        assertEquals(250L, lastRun)
    }

    @Test
    fun does_not_refire_while_still_silent() {
        repeat(300) { watchdog.feed(0f) }
        assertEquals(1, fired)                  // 触发一次后不重复刷屏
    }

    @Test
    fun non_zero_resets_the_run() {
        repeat(200) { watchdog.feed(0f) }
        watchdog.feed(1f)                       // 有声音 ⇒ 计数归零
        repeat(200) { watchdog.feed(0f) }
        assertEquals(0, fired)                  // 未到阈值，不该触发
    }

    @Test
    fun refires_after_silence_resumes() {
        repeat(260) { watchdog.feed(0f) }       // 触发一次
        watchdog.feed(3f)                       // 恢复有声 ⇒ 复位
        repeat(260) { watchdog.feed(0f) }       // 再静音 ⇒ 应再次触发
        assertEquals(2, fired)
    }

    @Test
    fun low_but_nonzero_rms_is_not_silence() {
        // 环境噪声实测 raw 39~72：**低电平 ≠ 静音**，判据是精确零
        repeat(300) { watchdog.feed(0.5f) }
        assertEquals(0, fired)
    }
}
