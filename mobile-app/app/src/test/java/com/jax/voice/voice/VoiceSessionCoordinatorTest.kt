package com.jax.voice.voice

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExecutorCoroutineDispatcher
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * VoiceSessionCoordinator 串行生命周期 L0 单测（SPEC §4.2 / ADR-016 / AC-05~07）。
 *
 * 覆盖：SIGNING 取消直接回 IDLE（不等待退房回调，修复永久退出锁）；ENTERING 取消进 EXITING；
 * 超时作为事件；重复 start/cancel 幂等；退出超时强制回 IDLE；旧 generation 迟到回调丢弃；
 * 快速点击 20 次只产生一个活动会话；Failure 按状态收敛。
 *
 * 反作弊：无 @Ignore/skip；真实 Channel + 单线程 dispatcher 串行消费；效果用 CompletableDeferred
 * 门控使中间态稳定可断言，未 mock 状态机本身。
 */
class VoiceSessionCoordinatorTest {

    private lateinit var dispatcher: ExecutorCoroutineDispatcher
    private lateinit var scope: CoroutineScope
    private lateinit var coordinator: VoiceSessionCoordinator

    private val signCalls = AtomicInteger(0)
    private val enterCalls = AtomicInteger(0)
    private val exitCalls = AtomicInteger(0)
    private val signGates = mutableMapOf<Long, CompletableDeferred<VoiceSessionInfo>>()
    private val signSources = mutableMapOf<Long, String>()
    private lateinit var exitGate: CompletableDeferred<Unit>

    private fun session(id: String) = VoiceSessionInfo(
        roomId = "room-$id", userId = "user-$id", userSig = "sig-$id",
        sdkAppId = 1600155678, sessionId = "sid-$id"
    )

    @Before
    fun setUp() {
        dispatcher = Executors.newSingleThreadExecutor { r ->
            Thread(r, "voice-coordinator-test").apply { isDaemon = true }
        }.asCoroutineDispatcher()
        scope = CoroutineScope(SupervisorJob() + dispatcher)
        signCalls.set(0); enterCalls.set(0); exitCalls.set(0)
        signGates.clear()
        signSources.clear()
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
        stubbornSign: Boolean = false
    ): VoiceSessionCoordinator {
        return VoiceSessionCoordinator(
            scope = scope,
            actorDispatcher = dispatcher,
            signSession = { gen, source ->
                signCalls.incrementAndGet()
                signSources[gen] = source
                val gate = CompletableDeferred<VoiceSessionInfo>()
                signGates[gen] = gate
                if (stubbornSign) withContext(NonCancellable) { gate.await() } else gate.await()
            },
            enterRoom = { _, _ -> enterCalls.incrementAndGet() },
            exitRoom = { _ -> exitCalls.incrementAndGet(); exitGate.await() },
            signTimeoutMs = signTimeoutMs,
            enterTimeoutMs = enterTimeoutMs,
            exitTimeoutMs = exitTimeoutMs
        )
    }

    private suspend fun awaitState(state: VoiceSessionState): VoiceSessionModel =
        withTimeout(3_000) { coordinator.model.first { it.state == state } }

    /** 等待签发 gate 注册（launchSign 与状态发布异步，避免取值竞态） */
    private suspend fun awaitSignGate(gen: Long): CompletableDeferred<VoiceSessionInfo> {
        withTimeout(3_000) { while (!signGates.containsKey(gen)) delay(5) }
        return signGates.getValue(gen)
    }

    private suspend fun awaitAnySignGate(): CompletableDeferred<VoiceSessionInfo> =
        awaitSignGate(1)

    /** 等待退房效果真正执行（launchExit 与状态发布异步，避免断言竞态） */
    private suspend fun awaitExitCalls(expected: Int) {
        withTimeout(3_000) { while (exitCalls.get() < expected) delay(5) }
    }

    @Test
    fun `accepted start source reaches signing effect unchanged`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("overlay")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate()
        assertEquals("overlay", signSources[1])
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)
    }

    // ---- AC-05: SIGNING 取消直接回 IDLE，不等待退房回调（修复永久退出锁）----
    @Test
    fun `signing cancel returns directly to IDLE without exit`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)
        assertEquals("SIGNING 取消不得触发退房", 0, exitCalls.get())
        // 迟到的签发结果必须被丢弃：不得进房
        awaitAnySignGate().complete(session("late"))
        delay(100)
        assertEquals(0, enterCalls.get())
        assertEquals(VoiceSessionState.IDLE, coordinator.model.value.state)
    }

    @Test
    fun `enter request returning does not report IN_ROOM before SDK success callback`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate().complete(session("async-enter"))
        awaitState(VoiceSessionState.ENTERING)
        withTimeout(3_000) { while (enterCalls.get() < 1) delay(5) }
        delay(100)

        assertEquals(
            "TRTC enterRoom 立即返回仅表示请求已提交，真实 onEnterRoom 成功回调前必须保持 ENTERING",
            VoiceSessionState.ENTERING,
            coordinator.model.value.state
        )
    }

    @Test
    fun `current generation SDK success is required to enter IN_ROOM`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate().complete(session("callback-success"))
        val entering = awaitState(VoiceSessionState.ENTERING)
        withTimeout(3_000) { while (enterCalls.get() < 1) delay(5) }

        coordinator.postEnterSucceeded(entering.generation)

        awaitState(VoiceSessionState.IN_ROOM)
    }

    // ---- AC-06: ENTERING 取消 → EXITING → 退房完成后 IDLE ----
    @Test
    fun `entering cancel goes to EXITING then IDLE after exit`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate().complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)
        coordinator.cancel()
        awaitState(VoiceSessionState.EXITING)
        awaitExitCalls(1)
        assertEquals("ENTERING 取消必须发起退出", 1, exitCalls.get())
        exitGate.complete(Unit)
        awaitState(VoiceSessionState.IDLE)
        assertEquals(1, exitCalls.get())
    }

    // ---- 超时作为事件：签发超时 → IDLE + error ----
    @Test
    fun `sign timeout is handled as event and returns to IDLE with error`() = runBlocking<Unit> {
        coordinator = buildCoordinator(signTimeoutMs = 40)
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        val m = awaitState(VoiceSessionState.IDLE)
        assertNotNull("签发超时必须带错误原因", m.error)
        assertTrue("签发超时错误信息缺失: ${m.error}", m.error!!.contains("签发超时"))
        assertEquals("签发超时不触发退房", 0, exitCalls.get())
    }

    // ---- 超时作为事件：进房超时 → EXITING → IDLE ----
    @Test
    fun `entering timeout goes to EXITING then IDLE`() = runBlocking<Unit> {
        coordinator = buildCoordinator(enterTimeoutMs = 40)
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate().complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)
        awaitState(VoiceSessionState.EXITING)
        awaitExitCalls(1)
        assertEquals("进房超时必须发起退出", 1, exitCalls.get())
        exitGate.complete(Unit)
        awaitState(VoiceSessionState.IDLE)
    }

    // ---- 重复 start/cancel 幂等（AC-07 非法转换不得静默执行）----
    @Test
    fun `repeated start and cancel are idempotent`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("a"); coordinator.start("b"); coordinator.start("c")
        awaitState(VoiceSessionState.SIGNING)
        delay(100)
        assertEquals("活动会话期间重复 start 只能签发一次", 1, signCalls.get())
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)
        coordinator.cancel() // IDLE 重复取消无操作
        coordinator.start("d")
        awaitState(VoiceSessionState.SIGNING)
        // 第二次会话的签发效果异步启动：等待其真正执行，避免断言竞态
        withTimeout(3_000) { while (signCalls.get() < 2) delay(5) }
        assertEquals("取消后再次 start 应产生新签发", 2, signCalls.get())
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)
    }

    // ---- 退出超时强制回 IDLE（退房回调缺失也不永久锁）----
    @Test
    fun `exit timeout forces IDLE instead of permanent exit lock`() = runBlocking<Unit> {
        coordinator = buildCoordinator(exitTimeoutMs = 40)
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate().complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)
        coordinator.cancel()
        awaitState(VoiceSessionState.EXITING)
        awaitExitCalls(1)
        assertEquals(1, exitCalls.get())
        // 退房回调永不返回 → 退出超时必须强制回 IDLE
        val m = awaitState(VoiceSessionState.IDLE)
        assertTrue("退出超时错误信息缺失: ${m.error}", m.error!!.contains("退出超时"))
    }

    // ---- 旧 generation 迟到回调丢弃（NonCancellable 阻止取消中断的顽固效果）----
    @Test
    fun `late callback from old generation is discarded`() = runBlocking<Unit> {
        coordinator = buildCoordinator(stubbornSign = true)
        coordinator.start("gen1")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)
        coordinator.start("gen2")
        awaitState(VoiceSessionState.SIGNING)
        // gen1 的签发结果迟到 → 必须被 generation 丢弃，不得进房
        awaitSignGate(1).complete(session("gen1-late"))
        delay(100)
        assertEquals("旧 generation 迟到回调不得进房", 0, enterCalls.get())
        assertEquals(VoiceSessionState.SIGNING, coordinator.model.value.state)
        // gen2 正常完成
        awaitSignGate(2).complete(session("gen2"))
        val entering = awaitState(VoiceSessionState.ENTERING)
        coordinator.postEnterSucceeded(entering.generation)
        awaitState(VoiceSessionState.IN_ROOM)
        assertEquals(1, enterCalls.get())
    }

    @Test
    fun `late SDK callbacks from old generation cannot revive current session`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("gen1")
        awaitState(VoiceSessionState.SIGNING)
        awaitSignGate(1).complete(session("gen1"))
        awaitState(VoiceSessionState.ENTERING)
        coordinator.cancel()
        awaitState(VoiceSessionState.EXITING)
        exitGate.complete(Unit)
        awaitState(VoiceSessionState.IDLE)

        exitGate = CompletableDeferred()
        coordinator.start("gen2")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.postEnterSucceeded(1)
        coordinator.postFailure(1, "late_enter", "旧会话失败")
        delay(100)

        assertEquals(VoiceSessionState.SIGNING, coordinator.model.value.state)
        assertEquals(2, coordinator.model.value.generation)
        assertEquals(null, coordinator.model.value.error)
    }

    // ---- 快速点击 20 次只产生一个活动会话 ----
    @Test
    fun `20 rapid taps produce exactly one active session`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        repeat(20) { coordinator.start("tap$it") }
        awaitState(VoiceSessionState.SIGNING)
        delay(100)
        assertEquals("快速点击只能产生一个活动会话", 1, signCalls.get())
        coordinator.cancel()
        awaitState(VoiceSessionState.IDLE)
        assertEquals(0, exitCalls.get())
    }

    // ---- Failure 在 SIGNING → IDLE + error ----
    @Test
    fun `failure during signing returns to IDLE with error`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        coordinator.postFailure(1, "auth_failed", "无法验证此设备")
        val m = awaitState(VoiceSessionState.IDLE)
        assertEquals("无法验证此设备", m.error)
        assertEquals(0, exitCalls.get())
    }

    // ---- Failure 在 ENTERING → EXITING → IDLE ----
    @Test
    fun `failure during entering exits then IDLE`() = runBlocking<Unit> {
        coordinator = buildCoordinator()
        coordinator.start("main")
        awaitState(VoiceSessionState.SIGNING)
        awaitAnySignGate().complete(session("s1"))
        awaitState(VoiceSessionState.ENTERING)
        coordinator.postFailure(1, "enter_timeout", "进房超时")
        awaitState(VoiceSessionState.EXITING)
        awaitExitCalls(1)
        assertEquals(1, exitCalls.get())
        exitGate.complete(Unit)
        awaitState(VoiceSessionState.IDLE)
    }
}
