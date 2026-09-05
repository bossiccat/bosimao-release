package com.jax.voice.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 悬浮窗权限重试闸门契约（2026-09-05 真机日志风暴驱动）：
 * 真机实测 MainActivity 每 40ms 调一次 show()，未授权时 25 条/秒 W 日志，
 * 30s 内 1000+ 条，把会话/sign/TRTC 的关键日志挤出 logcat 缓冲，导致无法取证。
 *
 * 期望：首次放行（事实至少记录一次）→ 间隔内节流 → 到点恢复（用户可能中途授权）。
 */
class OverlayPermissionGateTest {

    // 必须显式实现函数类型：Kotlin 只对 fun interface 做 SAM 转换，
    // 普通类的 operator fun invoke() 不会被当成 () -> Long。
    private class FakeClock(var now: Long = 0L) : () -> Long {
        override fun invoke(): Long = now
    }

    @Test
    fun `first attempt is allowed so the fact is logged once`() {
        val gate = OverlayPermissionGate(5_000L) { 1_000L }
        assertTrue("首次必须放行，否则未授权事实一条都不留", gate.shouldAttempt())
        assertEquals(0, gate.throttledCount)
    }

    @Test
    fun `repeated calls inside interval are throttled`() {
        val clock = FakeClock(1_000L)
        val gate = OverlayPermissionGate(5_000L, clock)
        assertTrue(gate.shouldAttempt())          // t=1000 放行，nextRetryAt=6000
        // 间隔内 124 次：40ms × 124 = 4960ms < 5000ms，全部落在窗口内。
        // 第 125 次恰好到 t=6000（边界），按「到点放行」处理，故不计入节流。
        repeat(124) {
            clock.now += 40L
            assertFalse("间隔内不得重复放行", gate.shouldAttempt())
        }
        assertEquals("被节流的次数必须如实累计", 124, gate.throttledCount)
    }

    @Test
    fun `attempt is allowed again after interval elapses`() {
        val clock = FakeClock(1_000L)
        val gate = OverlayPermissionGate(5_000L, clock)
        assertTrue(gate.shouldAttempt())
        repeat(124) { clock.now += 40L; gate.shouldAttempt() }
        clock.now += 40L // 累计 +5000ms，跨过间隔
        assertTrue("到点必须恢复放行（用户可能中途授权）", gate.shouldAttempt())
    }

    @Test
    fun `reset clears throttling immediately`() {
        val clock = FakeClock(1_000L)
        val gate = OverlayPermissionGate(5_000L, clock)
        assertTrue(gate.shouldAttempt())
        assertFalse(gate.shouldAttempt())
        gate.reset()
        assertTrue("授权后必须能立刻恢复，不该白等一个间隔", gate.shouldAttempt())
    }

    @Test
    fun `throttled bursts stay bounded on a long run`() {
        val clock = FakeClock(0L)
        val gate = OverlayPermissionGate(OverlayPermissionGate.DEFAULT_INTERVAL_MS, clock)
        var allowed = 0
        // 模拟 10 分钟、每 40ms 一次（真机前台常驻的真实量级）
        repeat(15_000) {
            clock.now += 40L
            if (gate.shouldAttempt()) allowed++
        }
        // 10min=600s / 5s 间隔 → 约 120 次放行，而不是 15000 次
        assertTrue("10 分钟只应放行约 120 次，实际 $allowed", allowed in 118..122)
    }
}
