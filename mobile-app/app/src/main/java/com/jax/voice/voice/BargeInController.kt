package com.jax.voice.voice

import android.os.SystemClock
import android.util.Log
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
 * - **播放态才打断**：非 SPEAKING/INTERRUPTED 一律忽略（点击与语音一视同仁）。
 *
 * 回声自激防护（2026-09-06 真机根治）：
 * 千问回复经扬声器外放被 mic 采回，[RtcAudioFrameRms] 判定「本地有人说话」→ 以
 * `source="user_voice"` 反复触发 [interrupt]，App 把自己的回复掐断。真机实测 117ms 内 5 连击、
 * 全程 32 次打断且 `source=tap` 为 0（用户一次都没点），播放段被切成 1.0~1.8s 的碎片，
 * 这就是用户听到的「间断性的卡」。
 *
 * 注：Android [android.media.audiofx.AcousticEchoCanceler] 是为 AudioSource.VOICE_COMMUNICATION
 * 设计的，本机采集用 AudioSource.MIC，没有回声参考信号，effect 创建成功但实际是空操作，
 * 因此只能在打断侧限流。防护由两道闸门组成，且**只对语音触发生效**，
 * `source="tap"`（用户显式点击）永不限制：
 * 1. [PLAYBACK_ONSET_GUARD_MS] 播放起始保护窗：刚开始播放时回声最强，实测自激首击在
 *    SPEAKING 后 ~10ms，本窗口可挡下绝大多数；
 * 2. 每个播放段只允许一次语音打断（[voiceInterruptUsedInSegment]）。
 */
class BargeInController(
    /** 本地播放 stop/flush（服务注入 RtcClient.interruptRemotePlayback；Task 7 脉冲 + generation） */
    private val interruptPlayback: () -> Unit,
    /** 体验状态发布（服务注入 VoiceController.publishExperience） */
    private val onExperience: (ExperienceState) -> Unit,
    /** 时钟注入（P95 计时用；测试可伪造） */
    private val nowMs: () -> Long = { System.currentTimeMillis() },
    /** 单调时钟注入（播放段边界与保护窗计时；测试可伪造，避免 JVM 单测依赖 SystemClock） */
    private val elapsedMs: () -> Long = { SystemClock.elapsedRealtime() }
) {

    companion object {
        private const val TAG = "BargeInCtrl"

        /** 语音触发的打断源（本地采集检出「用户开口」；回声自激也走这条路，故需限流） */
        internal const val SOURCE_USER_VOICE = "user_voice"

        /**
         * 播放起始保护窗（ms）：刚开始播放时回声最强、远端尚未稳定，这段时间内不接受语音打断。
         * 实测自激首击发生在 SPEAKING 后 ~10ms，本窗口能挡住绝大多数。
         */
        private const val PLAYBACK_ONSET_GUARD_MS = 400L

        /**
         * 语音打断额度自动恢复间隔（ms）：距上次语音打断超过该时长，视为已进入新的播放段并恢复额度。
         *
         * 这是「每播放段限一次」的**兜底**：段边界依赖外部 [onExperienceChange] 送来的 SPEAKING
         * 迁移事件，一旦该事件丢失（远端状态回调缺失等），额度会永久停在 true，语音打断将彻底失效
         * —— 那比自激更糟（用户喊也停不下来）。有了超时自动恢复，最坏情况只是自激多打一次，
         * 绝不会永久失效。
         */
        private const val VOICE_INTERRUPT_REARM_MS = 3_000L

        /** 忽略分支 logcat 降频窗口（ms）：自激时忽略次数可达每秒数十次，全打会日志风暴 */
        private const val IGNORE_LOG_THROTTLE_MS = 2_000L
    }
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

    /** 本次播放段是否已经用过语音打断。回声自激会在百毫秒内连打十几次，
     *  一个播放段只允许打断一次即可满足真实插话需求，同时彻底阻断自激。 */
    @Volatile
    private var voiceInterruptUsedInSegment = false

    /**
     * 本次播放段起始时间（[elapsedMs]），用于播放起始保护窗；-1 = 当前不在播放段。
     * 用 -1 而非 0 作哨兵：[elapsedMs] 在开机初期/伪造时钟下完全可以返回 0，
     * 拿 0 当「无」会让保护窗在段起始恰好为 0 时整体失效。
     */
    @Volatile
    private var speakingSinceMs = -1L

    /** 上次语音打断时刻（[elapsedMs]），用于额度超时自动恢复兜底 */
    @Volatile
    private var lastVoiceInterruptAtMs = 0L

    /** 忽略分支 logcat 降频状态 */
    @Volatile private var lastIgnoreLogAtMs = 0L
    @Volatile private var suppressedIgnoreCount = 0

    fun onExperienceChange(state: ExperienceState) {
        val prev = experience
        experience = state
        when {
            // 只在「进入」SPEAKING 时重置段边界：播放中被重复投递的 SPEAKING 不得刷新额度，
            // 否则自激每次重新拿到额度（真机自激正是靠 17ms 一次的 SPEAKING 重投实现的）
            state == ExperienceState.SPEAKING && prev != ExperienceState.SPEAKING -> {
                voiceInterruptUsedInSegment = false
                speakingSinceMs = elapsedMs()
            }
            prev == ExperienceState.SPEAKING && state != ExperienceState.SPEAKING -> {
                speakingSinceMs = -1L
            }
        }
    }

    /** 显式打断：仅 speaking（或进行中 interrupted）时执行一次，幂等 */
    fun interrupt(source: String) {
        if (experience != ExperienceState.SPEAKING && experience != ExperienceState.INTERRUPTED) {
            return // 幂等：非播放态忽略
        }
        if (interrupting) {
            return // 幂等：打断进行中忽略重复触发
        }
        // 回声自激闸门：只限制语音触发，用户显式点击（tap）永不限制
        if (source == SOURCE_USER_VOICE && !allowVoiceInterrupt()) return
        interrupting = true
        val t0 = nowMs()
        interruptGeneration++
        DiagLog.log("BargeIn", "interrupt source=$source gen=$interruptGeneration")
        // 成功打断必须进 logcat：此前 BargeIn 事件只写私有目录文件（DiagLog），
        // adb logcat 里计数恒为 0，排查「回复卡顿」时差点被误判成「根本没发生打断」。
        Log.w(TAG, "interrupt source=$source gen=$interruptGeneration")
        interruptPlayback() // 本地 stop/flush（同步返回；耗时计入 P95 样本）
        val t1 = nowMs()
        lastInterruptDurationMs = t1 - t0
        interruptCount++
        speakingSinceMs = -1L // 播放段已结束
        onExperience(ExperienceState.INTERRUPTED)
        onExperience(ExperienceState.LISTENING) // 打断后回 listening（实际恢复由远端事件细化）
        experience = ExperienceState.LISTENING
        interrupting = false
    }

    /**
     * 语音打断闸门（仅对 [SOURCE_USER_VOICE] 生效）。通过时顺带占用额度。
     *
     * 两道闸门 + 一道兜底，见 [VOICE_INTERRUPT_REARM_MS] 说明。
     */
    private fun allowVoiceInterrupt(): Boolean {
        val now = elapsedMs()

        // 兜底：额度超时自动恢复。段边界依赖外部 SPEAKING 迁移事件，事件丢失时
        // 没有这一步会导致语音打断永久失效（比自激更严重的故障）。
        if (voiceInterruptUsedInSegment && now - lastVoiceInterruptAtMs >= VOICE_INTERRUPT_REARM_MS) {
            voiceInterruptUsedInSegment = false
            DiagLog.log("BargeIn", "voice barge-in rearmed: ${now - lastVoiceInterruptAtMs}ms since last")
        }

        if (speakingSinceMs >= 0L && now - speakingSinceMs < PLAYBACK_ONSET_GUARD_MS) {
            logIgnored("onset guard ${now - speakingSinceMs}ms < ${PLAYBACK_ONSET_GUARD_MS}ms")
            return false
        }
        if (voiceInterruptUsedInSegment) {
            logIgnored("already used in this playback segment")
            return false
        }
        voiceInterruptUsedInSegment = true
        lastVoiceInterruptAtMs = now
        return true
    }

    /**
     * 忽略分支留痕：DiagLog 全量（自激取证要靠它），logcat 降频（自激时可达每秒数十次，
     * 全打会变成日志风暴，反而把关键日志淹掉）。
     */
    private fun logIgnored(reason: String) {
        DiagLog.log("BargeIn", "voice barge-in ignored: $reason")
        val now = elapsedMs()
        if (now - lastIgnoreLogAtMs >= IGNORE_LOG_THROTTLE_MS) {
            val suppressed = suppressedIgnoreCount
            suppressedIgnoreCount = 0
            lastIgnoreLogAtMs = now
            Log.i(TAG, "voice ignored: $reason" + if (suppressed > 0) " (+$suppressed suppressed)" else "")
        } else {
            suppressedIgnoreCount++
        }
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
