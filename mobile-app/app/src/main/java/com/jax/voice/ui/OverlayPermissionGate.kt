package com.jax.voice.ui

/**
 * 悬浮窗权限重试闸门（纯 JVM 可测：时钟与间隔均可注入）。
 *
 * 为什么要它（2026-09-05 真机实测，不是理论问题）：
 * MainActivity 的 UI 状态流每 40ms 回调一次并调用 FloatingOverlay.show()，
 * 未授予 SYSTEM_ALERT_WINDOW 时每次都会走「判定 + 打 W 日志」路径 —— 实测
 * 25 条/秒、30s 内 1000+ 条。后果有二：
 *  1. 占满 logcat 环形缓冲，把会话/sign/TRTC 的关键日志挤出去，真机问题无法取证
 *     （本次排查中曾据此误判为「点击后 App 完全没有日志」）；
 *  2. 持续无谓的判定与日志 IO，前台常驻服务白白耗电。
 *
 * 契约：首次调用放行（保证未授权事实至少记录一次），随后在 intervalMs 内一律
 * 节流；用户中途授权后再次调用自然恢复放行。节流次数对外可读，便于自检与测试。
 */
internal class OverlayPermissionGate(
    private val intervalMs: Long = DEFAULT_INTERVAL_MS,
    private val clock: () -> Long = { android.os.SystemClock.elapsedRealtime() },
) {

    /** 累计被节流的次数（未被节流的放行不计数） */
    var throttledCount: Int = 0
        private set

    private var nextRetryAtMs = 0L

    /** 本次是否应真正执行「判定 + 告警」 */
    fun shouldAttempt(): Boolean {
        if (clock() < nextRetryAtMs) {
            throttledCount++
            return false
        }
        nextRetryAtMs = clock() + intervalMs
        return true
    }

    /** 权限已授予/悬浮球已创建时复位，避免授权后仍吃一段节流 */
    fun reset() {
        nextRetryAtMs = 0L
    }

    companion object {
        const val DEFAULT_INTERVAL_MS = 5_000L
    }
}
