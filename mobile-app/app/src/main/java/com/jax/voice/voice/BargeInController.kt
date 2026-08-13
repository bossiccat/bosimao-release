package com.jax.voice.voice

import com.jax.voice.util.DiagLog

/**
 * 幂等打断控制器（SPEC AC-13/AC-14 / DESIGN-DETAIL §2.1 interrupted）。
 *
 * 语义：
 * - 用户开口或点击（[interrupt]）→ 先发布 INTERRUPTED，再切回 LISTENING（P0 目标：从动作到实际
 *   stop 播放 P95 ≤ 300ms，见 [lastInterruptDurationMs] 计时）。
 * - 打断只做本地播放 stop/flush + generation 失效（[interruptGeneration]），不改变长期远端订阅
 *   （Task 7 已保证正常远端停止不 mute；Task 8 接入 RtcClient.interruptRemotePlayback）。
 * - 旧 generation 下行帧（[shouldAcceptDownlink]）在打断后一律丢弃，禁止重新播放（AC-14）。
 * - 重复 pause/flush/interrupt 幂等：非目标态或进行中一律忽略，不产生重复副作用。
 */
class BargeInController(
    /** 本地播放 stop/flush（服务注入 RtcClient.interruptRemotePlayback；Task 7 脉冲 + generation） */
    private val interruptPlayback: () -> Unit,
    /** 体验状态发布（服务注入 VoiceController.publishExperience） */
    private val onExperience: (ExperienceState) -> Unit,
    /** 时钟注入（P95 计时用；测试可伪造） */
    private val nowMs: () -> Long = { System.currentTimeMillis() }
) {
    /** 打断代数：每次成功打断递增；下行帧/事件按此判旧（AC-14） */
    @Volatile
    var interruptGeneration: Int = 0
        private set

    /** 最近一次打断耗时（用户动作 → 本地 stop 播放完成，ms）；-1 表示尚未打断 */
    @Volatile
    var lastInterruptDurationMs: Long = -1L
        private set

    /** 打断总次数（P95 样本统计） */
    @Volatile
    var interruptCount: Int = 0
        private set

    @Volatile
    private var experience = ExperienceState.IDLE

    @Volatile
    private var interrupting = false

    fun onExperienceChange(state: ExperienceState) {
        experience = state
    }

    /** 显式打断：仅 speaking（或进行中 interrupted）时执行一次，幂等 */
    fun interrupt(source: String) {
        if (experience != ExperienceState.SPEAKING && experience != ExperienceState.INTERRUPTED) {
            return // 幂等：非播放态忽略
        }
        if (interrupting) {
            return // 幂等：打断进行中忽略重复触发
        }
        interrupting = true
        val t0 = nowMs()
        interruptGeneration++
        DiagLog.log("BargeIn", "interrupt source=$source gen=$interruptGeneration")
        interruptPlayback() // 本地 stop/flush（同步返回；耗时计入 P95 样本）
        val t1 = nowMs()
        lastInterruptDurationMs = t1 - t0
        interruptCount++
        onExperience(ExperienceState.INTERRUPTED)
        onExperience(ExperienceState.LISTENING) // 打断后回 listening（实际恢复由远端事件细化）
        experience = ExperienceState.LISTENING
        interrupting = false
    }

    /** 暂停（幂等）：仅 listening 下进入暂停；重复调用忽略 */
    fun pause() {
        if (experience != ExperienceState.LISTENING) return
        onExperience(ExperienceState.ENDPOINTING)
        experience = ExperienceState.ENDPOINTING
    }

    /** 恢复（幂等）：仅暂停态可恢复；重复调用忽略 */
    fun resume() {
        if (experience != ExperienceState.ENDPOINTING) return
        onExperience(ExperienceState.LISTENING)
        experience = ExperienceState.LISTENING
    }

    /** 冲刷（幂等）：清空本地缓冲不改变订阅；任意时刻可调用，重复调用无副作用 */
    fun flush() {
        DiagLog.log("BargeIn", "flush")
        interruptPlayback()
    }

    /** AC-14：打断后旧 generation 下行帧必须丢弃；新代数（含当前）才可接受 */
    fun shouldAcceptDownlink(gen: Long): Boolean = gen >= interruptGeneration

    /** AC-13：打断耗时是否满足 P95 ≤ 300ms（单元/真机验收共用判定） */
    fun lastInterruptWithinBudget(maxMs: Long = 300L): Boolean =
        lastInterruptDurationMs in 0..maxMs
}
