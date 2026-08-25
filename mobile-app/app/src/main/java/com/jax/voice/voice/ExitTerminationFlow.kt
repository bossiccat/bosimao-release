package com.jax.voice.voice

import kotlinx.coroutines.delay

/**
 * 退出前终止通知总编排（Task #23 上游接线，TDD 纯逻辑层）。
 *
 * 链路：EXITING → postTerminate 拿 termination_id → RtcClient.sendTerminationNotice(tid)
 *       （false 时短重试 [maxRetries] 次 × [retryDelayMs]）→ 返回后调用方再 exitRoom。
 *
 * 语义（fail-open）：
 * - 不在房（!inRoom）或无 sessionId → 直接跳过（返回 false），不发起任何网络/RTC 调用。
 * - postTerminate 抛错向上传播（由 VoiceForegroundService 吞掉后照常退房；CP 超时兜底已有）。
 * - sendNotice 持续 false → 重试耗尽后返回 false，不阻塞拆链。
 * - 总耗时预算：HTTP 由服务侧 callTimeout(600ms) 封顶 + 2×500ms 重试 ≈ ≤1.7s（< ~2s 上限，
   App 被杀场景下退出主路径不受阻）。
 */
internal object ExitTerminationFlow {

    /**
     * @param inRoom 是否仍在房间（IN_ROOM 才发通知）
     * @param signedSession 本次会话签发信息（sessionId 为空 = 无法对账，跳过）
     * @param postTerminate 调用方绑定好的 HTTP terminate 调用，成功返回 termination_id
     * @param sendNotice 绑定好的 RTC 自定义命令发送，true = 已发出
     */
    suspend fun runBeforeExit(
        inRoom: Boolean,
        signedSession: VoiceSessionInfo?,
        postTerminate: suspend () -> String,
        sendNotice: suspend (terminationId: String) -> Boolean,
        retryDelayMs: Long = 500L,
        maxRetries: Int = 2
    ): Boolean {
        if (!inRoom) return false
        val sessionId = signedSession?.sessionId
        if (sessionId.isNullOrBlank()) return false // 无 sessionId：照常退房（CP 兜底）
        val terminationId = postTerminate()
        var sent = sendNotice(terminationId)
        var attempts = 0
        while (!sent && attempts < maxRetries) {
            delay(retryDelayMs)
            sent = sendNotice(terminationId)
            attempts++
        }
        return sent
    }
}
