package com.jax.voice.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * BargeInController 幂等打断 L0 单测（Task 8 / SPEC AC-13 AC-14）。
 *
 * 覆盖：speaking → interrupted → listening；非播放态/重复打断幂等；
 * 重复 pause/flush 幂等（不产生 UI 事件、不改状态）；打断后旧 generation
 * 下行帧丢弃（AC-14）；打断耗时 P95 ≤ 300ms 计时与判定（注入时钟）。
 *
 * 反作弊：无 @Ignore/skip；用可伪造时钟做真实数值断言，不 mock 状态机本身。
 */
class BargeInControllerTest {

    private var now = 0L
    private var stopCalls = 0
    private val experiences = mutableListOf<ExperienceState>()

    private fun controller(stopCostMs: Long = 150L): BargeInController {
        now = 0L
        stopCalls = 0
        experiences.clear()
        return BargeInController(
            interruptPlayback = { stopCalls++; now += stopCostMs },
            onExperience = { experiences.add(it) },
            nowMs = { now }
        )
    }

    // ---- AC-13: speaking 打断 → interrupted → listening，本地 stop 一次 ----
    @Test
    fun `speaking interrupt publishes INTERRUPTED then LISTENING and stops playback once`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.SPEAKING)
        c.interrupt("tap")
        assertEquals(listOf(ExperienceState.INTERRUPTED, ExperienceState.LISTENING), experiences)
        assertEquals("必须恰好执行一次本地 stop/flush", 1, stopCalls)
        assertEquals("打断代数必须递增", 1, c.interruptGeneration)
    }

    // ---- 幂等：非播放态打断忽略 ----
    @Test
    fun `interrupt outside speaking is idempotent no-op`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.LISTENING)
        c.interrupt("tap")
        assertEquals(0, stopCalls)
        assertEquals(0, c.interruptGeneration)
        assertTrue("非播放态打断不得发布任何体验事件", experiences.isEmpty())

        c.onExperienceChange(ExperienceState.IDLE)
        c.interrupt("tap")
        assertEquals(0, stopCalls)
    }

    // ---- 幂等：重复打断只执行一次 ----
    @Test
    fun `repeated interrupt executes stop only once`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.SPEAKING)
        c.interrupt("a")
        c.interrupt("b")
        c.interrupt("c")
        assertEquals("重复打断必须幂等", 1, stopCalls)
        assertEquals(1, c.interruptGeneration)
    }

    // ---- 幂等：重复 pause/resume/flush 不产生重复 UI 事件 ----
    @Test
    fun `repeated pause resume and flush are idempotent on events and state`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.LISTENING)
        c.pause()
        c.pause()
        assertEquals(listOf(ExperienceState.ENDPOINTING), experiences)
        c.resume()
        c.resume()
        assertEquals(listOf(ExperienceState.ENDPOINTING, ExperienceState.LISTENING), experiences)
        c.flush()
        c.flush()
        // flush 不改变体验状态、不发布事件、不递增打断代数
        assertEquals(listOf(ExperienceState.ENDPOINTING, ExperienceState.LISTENING), experiences)
        assertEquals(0, c.interruptGeneration)
        // 非 listening 态 pause 忽略
        c.onExperienceChange(ExperienceState.SPEAKING)
        c.pause()
        assertEquals(2, experiences.size)
    }

    // ---- AC-14: 打断后旧 generation 下行帧丢弃 ----
    @Test
    fun `barge in invalidates old generation downlink frames`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.SPEAKING)
        assertTrue("打断前旧代数帧可接受", c.shouldAcceptDownlink(0))
        c.interrupt("tap")
        assertFalse("打断前代数的下行帧必须丢弃（AC-14）", c.shouldAcceptDownlink(0))
        assertTrue("打断后新代数帧可接受", c.shouldAcceptDownlink(c.interruptGeneration.toLong()))
    }

    // ---- AC-13: 打断耗时 P95 ≤ 300ms（用户动作 → 本地 stop 完成）----
    @Test
    fun `interrupt duration is recorded and within 300ms budget`() {
        val c = controller(stopCostMs = 150L)
        c.onExperienceChange(ExperienceState.SPEAKING)
        now = 1_000L
        c.interrupt("tap")
        assertEquals("耗时 = stop 完成时刻 - 用户动作时刻", 150L, c.lastInterruptDurationMs)
        assertTrue("打断耗时必须在 P95 ≤ 300ms 预算内", c.lastInterruptWithinBudget())
        assertEquals(1, c.interruptCount)
    }

    @Test
    fun `interrupt exceeding 300ms budget is flagged`() {
        val c = controller(stopCostMs = 350L)
        c.onExperienceChange(ExperienceState.SPEAKING)
        c.interrupt("tap")
        assertEquals(350L, c.lastInterruptDurationMs)
        assertFalse("超过 300ms 预算必须被标记（真机 P95 采集）", c.lastInterruptWithinBudget())
    }
}
