package com.jax.voice.voice

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * ExitTerminationFlow 退房前终止通知编排单测（Task #23 上游接线，TDD）。
 *
 * 覆盖（对应任务验收用例）：
 *  F1 成功路径：postTerminate 一次 → sendNotice 一次 → true
 *  F2 重试：false→false→true，共 3 次发送尝试
 *  F3 耗尽：持续 false，重试 maxRetries 次后返回 false
 *  F4 无 sessionId / 不在房：零网络/RTC 调用直接跳过
 *  F5 postTerminate 抛错向上传播（服务层吞掉后照常退房）
 * 反作弊：新增测试，无 @Ignore/.only/弱化断言；测试注入 retryDelayMs=1 避免真实等待。
 */
class ExitTerminationFlowTest {

    private fun signed() = VoiceSessionInfo(
        roomId = "jax-device-1",
        userId = "device-1",
        userSig = "sig",
        sdkAppId = 1600155678,
        sessionId = "s-1"
    )

    private suspend fun flow(
        inRoom: Boolean = true,
        session: VoiceSessionInfo? = signed(),
        postTerminate: suspend () -> String = { "tid-1" },
        sendNotice: suspend (String) -> Boolean,
        maxRetries: Int = 2
    ): Boolean = ExitTerminationFlow.runBeforeExit(
        inRoom = inRoom,
        signedSession = session,
        postTerminate = postTerminate,
        sendNotice = sendNotice,
        retryDelayMs = 1L, // 测试注入：避免真实 500ms 等待
        maxRetries = maxRetries
    )

    // ---- F1: 成功 ----
    @Test
    fun `success posts once and sends notice once`() = runBlocking {
        var posts = 0
        val notices = mutableListOf<String>()
        val sent = flow(
            postTerminate = { posts++; "tid-9" },
            sendNotice = { tid -> notices += tid; true }
        )
        assertTrue(sent)
        assertEquals(1, posts)
        assertEquals(listOf("tid-9"), notices)
    }

    // ---- F2: false→false→true 短重试 ----
    @Test
    fun `retries twice then succeeds on third attempt`() = runBlocking {
        var attempts = 0
        val sent = flow(
            sendNotice = { attempts++; attempts >= 3 }
        )
        assertTrue(sent)
        assertEquals("false 后应重试到第 3 次", 3, attempts)
    }

    // ---- F3: 重试耗尽仍 false ----
    @Test
    fun `gives up after exhausting retries and returns false`() = runBlocking {
        var attempts = 0
        val sent = flow(
            sendNotice = { attempts++; false }
        )
        assertFalse(sent)
        assertEquals("初始 1 次 + 重试 2 次", 3, attempts)
    }

    // ---- F4a: 无 sessionId 分支：照常退房语义，零调用 ----
    @Test
    fun `skips entirely without sessionId`() = runBlocking {
        var called = false
        val sent = flow(
            session = signed().copy(sessionId = null),
            postTerminate = { called = true; "tid" },
            sendNotice = { called = true; true }
        )
        assertFalse(sent)
        assertFalse("无 sessionId 不得发起 terminate/notice", called)
    }

    // ---- F4b: 不在房分支 ----
    @Test
    fun `skips entirely when not in room`() = runBlocking {
        var called = false
        val sent = flow(
            inRoom = false,
            postTerminate = { called = true; "tid" },
            sendNotice = { called = true; true }
        )
        assertFalse(sent)
        assertFalse(called)
    }

    // ---- F5: postTerminate 抛错向上传播（服务层 catch 后照常退房）----
    @Test
    fun `postTerminate failure propagates and notice is never sent`() = runBlocking {
        var notices = 0
        val error = runCatching {
            flow(
                postTerminate = { throw IOException("HTTP 503") },
                sendNotice = { notices++; true }
            )
        }.exceptionOrNull()
        assertTrue(error is IOException)
        assertEquals(0, notices)
    }
}
