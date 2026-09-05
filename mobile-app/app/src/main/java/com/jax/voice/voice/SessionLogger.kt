package com.jax.voice.voice

import android.util.Log

/**
 * 会话协调器日志出口（可注入）。
 *
 * 为什么不让 [VoiceSessionCoordinator] 直接调 android.util.Log：
 * 本项目跑 JVM 单测，虽然 app/build.gradle.kts 开了 `unitTests.isReturnDefaultValues = true`
 * （Log.* 静默返回而不抛 "not mocked"），但那样日志就成了不可断言的黑洞 ——
 * 日志字段哪天悄悄失效，测试发现不了。注入记录器后可以断言日志内容。
 *
 * 更重要的动机是**真机可观测性**：2026-09-05 真机排查「点了立即对话没反应」时，
 * 整个会话生命周期在 logcat 里一行日志都没有，只能靠 dumpsys 读界面上的 tvLastError
 * 文本反推错误（当时是 `Failed to connect to localhost/127.0.0.1:8443`）。
 * 状态机每一次迁移、失败、超时、冲突都必须留痕，否则真机问题无法取证。
 */
internal enum class SessionLogLevel { D, I, W, E }

internal fun interface SessionLogger {
    fun log(level: SessionLogLevel, tag: String, message: String, error: Throwable?)
}

internal fun SessionLogger.d(tag: String, message: String) =
    log(SessionLogLevel.D, tag, message, null)

internal fun SessionLogger.i(tag: String, message: String) =
    log(SessionLogLevel.I, tag, message, null)

internal fun SessionLogger.w(tag: String, message: String, error: Throwable? = null) =
    log(SessionLogLevel.W, tag, message, error)

internal fun SessionLogger.e(tag: String, message: String, error: Throwable? = null) =
    log(SessionLogLevel.E, tag, message, error)

/** 生产实现：转发到 logcat。真机过滤 `adb logcat -s VoiceSessionCoord:*`。 */
internal object AndroidSessionLogger : SessionLogger {
    override fun log(level: SessionLogLevel, tag: String, message: String, error: Throwable?) {
        when (level) {
            SessionLogLevel.D -> Log.d(tag, message, error)
            SessionLogLevel.I -> Log.i(tag, message, error)
            SessionLogLevel.W -> Log.w(tag, message, error)
            SessionLogLevel.E -> Log.e(tag, message, error)
        }
    }
}
