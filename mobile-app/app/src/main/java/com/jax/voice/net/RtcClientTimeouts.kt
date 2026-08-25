package com.jax.voice.net

internal class RtcClientTimeouts(
    private val exitTimeoutMs: Long,
    private val enterTimeoutMs: Long,
    private val remoteLeaveTimeoutMs: Long,
    private val onExitTimeout: () -> Unit,
    private val onEnterTimeout: () -> Unit,
    private val onRemoteLeaveTimeout: () -> Unit,
) {
    @Volatile var exitThread: Thread? = null
        private set
    @Volatile var enterThread: Thread? = null
        private set
    @Volatile var leaveThread: Thread? = null
        private set

    fun scheduleExit() { cancelExit(); exitThread = daemonDelay(exitTimeoutMs, onExitTimeout) }
    fun cancelExit() { exitThread?.interrupt(); exitThread = null }
    fun scheduleEnter() { cancelEnter(); enterThread = daemonDelay(enterTimeoutMs, onEnterTimeout) }
    fun cancelEnter() { enterThread?.interrupt(); enterThread = null }
    fun scheduleLeave() { cancelLeave(); leaveThread = daemonDelay(remoteLeaveTimeoutMs, onRemoteLeaveTimeout) }
    fun cancelLeave() { leaveThread?.interrupt(); leaveThread = null }

    private fun daemonDelay(ms: Long, onTimeout: () -> Unit): Thread = Thread {
        try {
            Thread.sleep(ms)
            onTimeout()
        } catch (_: InterruptedException) {
            // 正常回调先到，兜底已取消
        }
    }.apply { isDaemon = true; start() }
}
