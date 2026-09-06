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
 * 覆盖回声自激防护（2026-09-06）：播放起始保护窗、每播放段一次语音打断额度、
 * 额度超时自动恢复兜底，以及 tap（用户显式点击）不受限流影响。
 *
 * 反作弊：无 @Ignore/skip；用可伪造时钟做真实数值断言，不 mock 状态机本身。
 */
class BargeInControllerTest {

    private var now = 0L
    /** 单调时钟（播放段边界/保护窗用）：与 nowMs 分离，避免 stop 耗时污染段计时 */
    private var elapsed = 0L
    private var stopCalls = 0
    private val experiences = mutableListOf<ExperienceState>()

    private fun controller(stopCostMs: Long = 150L): BargeInController {
        now = 0L
        elapsed = 0L
        stopCalls = 0
        experiences.clear()
        return BargeInController(
            interruptPlayback = { stopCalls++; now += stopCostMs },
            onExperience = { experiences.add(it) },
            nowMs = { now },
            elapsedMs = { elapsed }
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

    // ---- 回声自激防护（真机 117ms 内 5 连击、32 次打断全部 source=user_voice）----

    @Test
    fun `voice barge-in fires only once per playback segment`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.SPEAKING)
        elapsed = 1_000L // 越过 400ms 起始保护窗
        // 自激实测形态：百毫秒内连打 5 次
        repeat(5) { c.interrupt(BargeInController.SOURCE_USER_VOICE) }
        assertEquals("一个播放段内语音打断只允许生效一次", 1, stopCalls)
        assertEquals(1, c.interruptGeneration)

        // 隔离验证「额度」本身：把体验态保持在允许打断的 INTERRUPTED（且不重投 SPEAKING，
        // 即不触发段边界重置），此时拦住第二次的只能是额度，不是既有的状态守卫
        c.onExperienceChange(ExperienceState.INTERRUPTED)
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals("额度未恢复时语音打断必须被拦下", 1, stopCalls)

        // 离开 SPEAKING 再进入 = 新播放段，额度恢复
        c.onExperienceChange(ExperienceState.LISTENING)
        c.onExperienceChange(ExperienceState.SPEAKING)
        elapsed = 5_000L
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals("新播放段必须恢复语音打断额度", 2, stopCalls)
    }

    @Test
    fun `voice barge-in is ignored inside playback onset guard window`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.SPEAKING) // speakingSinceMs = 0
        elapsed = 10L // 自激首击实测在 SPEAKING 后 ~10ms
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals("播放起始保护窗内的语音打断必须被忽略", 0, stopCalls)

        elapsed = 150L // 仍在 400ms 窗内
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals(0, stopCalls)

        elapsed = 500L // 越过保护窗，可以打断
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals("保护窗过后语音打断必须生效", 1, stopCalls)
    }

    @Test
    fun `tap is never rate limited and voice quota rearms after idle when SPEAKING event is lost`() {
        val c = controller()
        c.onExperienceChange(ExperienceState.SPEAKING)
        elapsed = 1_000L

        // tap 不受任何限流：连点 3 次，每次播放态重新进入后都能打断
        c.interrupt("tap")
        c.onExperienceChange(ExperienceState.SPEAKING)
        c.interrupt("tap")
        c.onExperienceChange(ExperienceState.SPEAKING)
        c.interrupt("tap")
        assertEquals("用户显式点击必须永不被限流", 3, stopCalls)

        // 兜底：段边界事件丢失（此后不再重投 SPEAKING）时，额度必须超时自动恢复，
        // 否则语音打断会永久失效——那比自激更糟（用户喊也停不下来）
        c.onExperienceChange(ExperienceState.SPEAKING)
        elapsed = 2_000L
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals(4, stopCalls)

        // 保持在允许打断的体验态，使拦截面收敛到额度/兜底逻辑本身
        c.onExperienceChange(ExperienceState.INTERRUPTED)
        elapsed = 2_500L // 距上次仅 500ms，未过 3000ms 恢复间隔 → 仍被限额
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals(4, stopCalls)
        elapsed = 6_000L // 距上次 4000ms > 3000ms → 自动恢复（即使 SPEAKING 事件从未重投）
        c.interrupt(BargeInController.SOURCE_USER_VOICE)
        assertEquals("段边界事件丢失时额度必须自动恢复", 5, stopCalls)
    }
}
