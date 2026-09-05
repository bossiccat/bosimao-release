package com.jax.voice.voice

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExecutorCoroutineDispatcher
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

/**
 * VoiceSessionCoordinator 可观测性契约（2026-09-05 真机取证驱动）。
 *
 * 为什么要这份测试：真机排查「点了立即对话没反应」时，整个会话生命周期在 logcat 里
 * 只有一条无关的 `mic restarted after session`，真正的 `Failed to connect to
 * localhost/127.0.0.1:8443` 只能靠 `adb shell dumpsys` 读界面 tvLastError 反推 ——
 * 因为状态机零日志。埋点加上之后，必须有测试盯住**日志字段**，否则哪天字段被改掉
 * （比如 generation 不再打印），日志还在、信息没了，测试却全绿。
 *
 * 因此每条断言都针对**字段内容**（generation / sessionId / code / state / Throwable），
 * 而不是「有没有日志」。
 *
 * 反作弊：无 @Ignore/skip；真实 Channel + 单线程 dispatcher 串行消费；效果用
 * CompletableDeferred 门控使中间态稳定可断言；日志出口用注入的记录器，未 mock 状态机。
 */
class VoiceSessionCoordinatorLoggingTest {

    /**
     * 日志过滤名硬编码在此而非引用常量：这既是断言，也是契约锁定 ——
     * 真机取证用的就是 `adb logcat -s VoiceSessionCoord:*`，改了它运维手册就失效。
     */
    private val expectedTag = "VoiceSessionCoord"

    private data class LogEntry(
        val level: SessionLogLevel,
        val tag: String,
        val message: String,
        val error: Throwable?
    )

    /** 记录器：SessionLogger 是可注入的 fun interface，这里把 log() 落到列表供断言 */
    private class RecordingLogger : SessionLogger {
        // 效果协程与 actor 协程都可能写日志，必须线程安全
        val entries = CopyOnWriteArrayList<LogEntry>()

        override fun log(level: SessionLogLevel, tag: String, message: String, error: Throwable?) {
            entries.add(LogEntry(level, tag, message, error))
        }

        fun messages(): List<String> = entries.map { it.message }
    }

    private lateinit var dispatcher: ExecutorCoroutineDispatcher
    private lateinit var scope: CoroutineScope
    private lateinit var coordinator: VoiceSessionCoordinator
    private lateinit var logger: RecordingLogger

    private val signGates = mutableMapOf<Long, CompletableDeferred<VoiceSessionInfo>>()
    private lateinit var enterGate: CompletableDeferred<Unit>
    private lateinit var exitGate: CompletableDeferred<Unit>

    private fun session(id: String, expiresAtEpochMs: Long = System.currentTimeMillis() + 600_000L) = VoiceSessionInfo(
        roomId = "room-$id", userId = "user-$id", userSig = "sig-$id",
        sdkAppId = 1600155678, sessionId = "sid-$id", expiresAtEpochMs = expiresAtEpochMs
    )

    @Before
    fun setUp() {
        dispatcher = Executors.newSingleThreadExecutor { r ->
            Thread(r, "voice-coordinator-logging-test").apply { isDaemon = true }
        }.asCoroutineDispatcher()
        scope = CoroutineScope(SupervisorJob() + dispatcher)
        logger = RecordingLogger()
        signGates.clear()
        enterGate = CompletableDeferred()
        exitGate = CompletableDeferred()
    }

    @After
    fun tearDown() {
        scope.cancel()
        dispatcher.close()
    }

    private fun buildCoordinator(
        signTimeoutMs: Long = 10_000L,
        enterTimeoutMs: Long = 15_000L,
        exitTimeoutMs: Long = 5_000L,
        failingSign: Throwable? = null,
        failingEnter: Throwable? = null
    ): VoiceSessionCoordinator {
        return VoiceSessionCoordinator(
            scope = scope,
            actorDispatcher = dispatcher,
            signSession = { gen, _ ->
                failingSign?.let { throw it }
                val gate = CompletableDeferred<VoiceSessionInfo>()
                signGates[gen] = gate
                gate.await()
            },
            enterRoom = { _, _ ->
                failingEnter?.let { throw it }
                enterGate.await()
            },
            exitRoom = { _ -> exitGate.await() },
            signTimeoutMs = signTimeoutMs,
            enterTimeoutMs = enterTimeoutMs,
            exitTimeoutMs = exitTimeoutMs,
            logger = logger
        )
    }

    private suspend fun awaitState(state: VoiceSessionState): VoiceSessionModel =
        withTimeout(3_000) { coordinator.model.first { it.state == state } }

    private suspend fun awaitSignGate(gen: Long): CompletableDeferred<VoiceSessionInfo> {
        withTimeout(3_000) { while (!signGates.containsKey(gen)) delay(5) }
        return signGates.getValue(gen)
    }

    /** 轮询等待日志出现：日志在 publish 之前写入，但效果协程是异步的，避免取值竞态 */
    private suspend fun awaitLogged(substring: String): LogEntry = withTimeout(3_000L) {
        var hit: LogEntry? = null
        while (hit == null) {
            hit = logger.entries.firstOrNull { it.message.contains(substring) }
            if (hit == null) delay(5L)
        }
        hit!!
    }

    private fun assertTagged(entry: LogEntry) {
        assertEquals(
            "日志 tag 必须是 $expectedTag，否则 `adb logcat -s $expectedTag:*` 抓不到: ${entry.message}",
            expectedTag,
            entry.tag
        )
    }

    // ---- 完整成功路径：Start → SignSucceeded → EnterSucceeded 每步都要带 generation / sessionId ----
    @Test
    fun `happy path logs every transition with generation and sessionId`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("overlay")
        awaitState(VoiceSessionState.SIGNING)
        val startEntry = awaitLogged("start accepted")
        assertTagged(startEntry)
        assertEquals("start 应记 I 级", SessionLogLevel.I, startEntry.level)
        assertTrue("start 日志缺 source: ${startEntry.message}", startEntry.message.contains("source=overlay"))
        assertTrue("start 日志缺 generation: ${startEntry.message}", startEntry.message.contains("generation=1"))

        awaitSignGate(1).complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)
        val signEntry = awaitLogged("sign succeeded")
        assertTagged(signEntry)
        assertTrue("sign 日志缺 generation: ${signEntry.message}", signEntry.message.contains("generation=1"))
        assertTrue("sign 日志缺 sessionId: ${signEntry.message}", signEntry.message.contains("sessionId=sid-s1"))

        enterGate.complete(Unit)
        coordinator.postEnterSucceeded(1)
        awaitState(VoiceSessionState.IN_ROOM)
        val enterEntry = awaitLogged("enter succeeded")
        assertTagged(enterEntry)
        assertTrue("enter 日志缺 generation: ${enterEntry.message}", enterEntry.message.contains("generation=1"))

        // 全链路 tag 一致性：任何一条跑偏都会让 logcat 过滤漏掉关键行
        assertTrue("存在非法 tag 的日志: ${logger.entries.filter { it.tag != expectedTag }}",
            logger.entries.all { it.tag == expectedTag })
    }

    // ---- Failure：code / message / generation / state 缺一不可 ----
    @Test
    fun `failure logs code message generation and state`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.postFailure("auth_failed", "无法验证此设备")
        awaitState(VoiceSessionState.IDLE)

        val entry = awaitLogged("failure code=")
        assertTagged(entry)
        assertEquals("failure 应记 W 级", SessionLogLevel.W, entry.level)
        assertTrue("failure 日志缺 code: ${entry.message}", entry.message.contains("code=auth_failed"))
        assertTrue("failure 日志缺 message: ${entry.message}", entry.message.contains("message=无法验证此设备"))
        assertTrue("failure 日志缺 generation: ${entry.message}", entry.message.contains("generation=1"))
        assertTrue("failure 日志缺 state: ${entry.message}", entry.message.contains("state=SIGNING"))
    }

    // ---- 真实取证场景：签发网络失败（Failed to connect ...）必须能直接从日志看出 code 与原因 ----
    @Test
    fun `sign network failure is visible in logs with code and reason`() = runBlocking<Unit> {
        coordinator = buildCoordinator(
            failingSign = IllegalStateException("Failed to connect to localhost/127.0.0.1:8443")
        )

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        val failure = awaitLogged("failure code=sign_failed")
        assertTrue("真实失败原因必须留在日志里: ${failure.message}", failure.message.contains("127.0.0.1:8443"))
        assertTrue("必须带 generation: ${failure.message}", failure.message.contains("generation=1"))
    }

    // ---- Timeout：当前 state + generation ----
    @Test
    fun `timeout logs current state and generation`() = runBlocking<Unit> {
        coordinator = buildCoordinator(signTimeoutMs = 40)

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitState(VoiceSessionState.IDLE)

        val entry = awaitLogged("timeout phase=")
        assertTagged(entry)
        assertEquals("timeout 应记 W 级", SessionLogLevel.W, entry.level)
        assertTrue("timeout 日志缺 phase/state: ${entry.message}", entry.message.contains("phase=SIGNING"))
        assertTrue("timeout 日志缺 state: ${entry.message}", entry.message.contains("state=SIGNING"))
        assertTrue("timeout 日志缺 generation: ${entry.message}", entry.message.contains("generation=1"))
    }

    // ---- Cancel：当前 state ----
    @Test
    fun `cancel logs current state`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)

        val entry = awaitLogged("cancel requested")
        assertTagged(entry)
        assertTrue("cancel 日志缺 state: ${entry.message}", entry.message.contains("state=SIGNING"))
        assertTrue("cancel 日志缺 generation: ${entry.message}", entry.message.contains("generation=1"))
    }

    // ---- recordConflict 不再静默：非法转换必须落日志 ----
    @Test
    fun `conflict is logged instead of silently swallowed`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("first")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.start("second") // SIGNING 期间重复 start → 记录冲突

        val entry = awaitLogged("conflict")
        assertTagged(entry)
        assertEquals("conflict 应记 W 级", SessionLogLevel.W, entry.level)
        assertTrue("conflict 日志必须含冲突描述: ${entry.message}", entry.message.contains("start(second) while SIGNING"))
        assertEquals("冲突计数仍需递增", 1, coordinator.conflicts)
    }

    // ---- 效果异常：E 级 + 带 Throwable（原来只吞进 message，堆栈全丢）----
    @Test
    fun `sign effect exception is logged at E level with throwable`() = runBlocking<Unit> {
        val boom = IllegalStateException("Failed to connect to localhost/127.0.0.1:8443")
        coordinator = buildCoordinator(failingSign = boom)

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)

        val entry = awaitLogged("sign effect failed")
        assertTagged(entry)
        assertEquals("效果异常必须记 E 级", SessionLogLevel.E, entry.level)
        assertTrue("必须带 generation: ${entry.message}", entry.message.contains("generation=1"))
        assertNotNull("E 级日志必须带 Throwable，否则堆栈丢失: ${entry.message}", entry.error)
        assertEquals("挂的必须是原始异常", boom, entry.error)
        assertTrue("异常信息必须留在日志里: ${entry.message}", entry.message.contains("127.0.0.1:8443"))
    }

    @Test
    fun `enter effect exception is logged at E level with throwable`() = runBlocking<Unit> {
        val boom = IllegalStateException("enter room rejected")
        coordinator = buildCoordinator(failingEnter = boom)

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitSignGate(1).complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)

        val entry = awaitLogged("enter effect failed")
        assertTagged(entry)
        assertEquals("效果异常必须记 E 级", SessionLogLevel.E, entry.level)
        assertTrue("必须带 generation: ${entry.message}", entry.message.contains("generation=1"))
        assertNotNull("E 级日志必须带 Throwable: ${entry.message}", entry.error)
        assertEquals("挂的必须是原始异常", boom, entry.error)
    }

    // ---- ExitSucceeded：回 IDLE 这一步同样不能没有日志 ----
    @Test
    fun `exit succeeded logs generation`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitSignGate(1).complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)
        coordinator.cancel()
        awaitState(VoiceSessionState.EXITING)
        exitGate.complete(Unit)
        awaitState(VoiceSessionState.IDLE)

        val entry = awaitLogged("exit succeeded")
        assertTagged(entry)
        assertTrue("exit 日志缺 generation: ${entry.message}", entry.message.contains("generation=1"))
    }

    // ---- 入口入队：区分「事件没进队列」与「进了队列被状态机拒绝」——「点了没反应」的第一道分水岭 ----
    @Test
    fun `start enqueue is logged before the state machine can reject it`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("notification_talk")
        val entry = awaitLogged("start requested")
        assertTagged(entry)
        assertTrue("入队日志必须带 source（三入口归因）: ${entry.message}", entry.message.contains("source=notification_talk"))
        assertTrue("入队日志必须带入队瞬间的 generation: ${entry.message}", entry.message.contains("currentGeneration=0"))

        // 若状态机随后接受了，还会有一条 start accepted —— 两条并存才能定位"没反应"卡在哪
        awaitState(VoiceSessionState.SIGNING)
        awaitLogged("start accepted")
    }

    /**
     * postFailure 是唯一一个「generation 由调用方携带、却在发送时才读取当前值」的入口：
     * 外部只持有旧 gen 快照，方法内读的是 Coordinator 当前 gen，不一致就会被判为陈旧丢弃。
     * 两个值都必须落日志，否则真机上「失败了却什么都没发生」完全无从查起。
     */
    @Test
    fun `postFailure enqueue logs both stamped and current generation`() = runBlocking<Unit> {
        coordinator = buildCoordinator()

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.postFailure("rtc_error", "boom")

        val entry = awaitLogged("failure enqueued")
        assertTagged(entry)
        assertTrue("必须记录调用方携带的 generation: ${entry.message}", entry.message.contains("stampedGeneration=1"))
        assertTrue(
            "必须记录 Coordinator 当前 generation，否则无法判断是否会被判为陈旧丢弃: ${entry.message}",
            entry.message.contains("currentGeneration=1")
        )
    }

    /**
     * 凭证把关：日志会被导出、贴到工单和聊天里，userSig 一旦进日志就是长期泄露。
     * 这条是给"顺手多打一点字段"的后续修改设的闸。
     */
    @Test
    fun `logs never contain credentials`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        val s = session("s1")

        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitSignGate(1).complete(s)
        awaitState(VoiceSessionState.ENTERING)
        enterGate.complete(Unit)
        coordinator.postEnterSucceeded(1)
        awaitState(VoiceSessionState.IN_ROOM)
        awaitLogged("enter succeeded")

        val leaks = logger.entries.filter { it.message.contains(s.userSig) || it.message.contains("userSig=") }
        assertTrue("日志不得输出 userSig（日志会被导出/共享）: ${leaks.map { it.message }}", leaks.isEmpty())

        val literalLeaks = logger.entries.filter { "sig-" in it.message }
        assertTrue("日志不得出现 userSig 字面量: ${literalLeaks.map { it.message }}", literalLeaks.isEmpty())
    }
}
