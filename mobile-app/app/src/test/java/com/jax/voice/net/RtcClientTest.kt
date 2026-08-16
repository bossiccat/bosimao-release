package com.jax.voice.net

import android.content.Context
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoicePhase
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudDef
import com.tencent.trtc.TRTCCloudListener
import io.mockk.every
import io.mockk.mockk
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * RtcClient 状态机 L0 单测（mock TRTC SDK，不连真实 RTC 云）。
 *
 * 依据：docs/rtc-rebuild/RTC-CLIENT-TEST-DESIGN.md §2（六态状态机 + 连接状态）/ QA-PLAN §4.1 / ADR-012。
 * 防线：回调触发点按 SDK 真实回调名接线（javap 核对 13.4.0.20477 jar 通过），断言语义事件不硬编码回调名。
 * 反作弊：本文件为新增测试，无 @Ignore/.only/弱化断言。
 *
 * 覆盖（对应测试设计用例号）：
 *  S1 进房成功→CONNECTED / S2 进房失败→错误态 / S3 重复进房幂等
 *  R1 断线→重连回调链 / R2 重连失败保持重试
 *  E1 正常退房→onExitRoom→onExited / E2 退房前不触发 onExited（等回调，mic handoff 防竞态）
 *  H1 mic handoff 标志（会话期 onExited 不触发 = MicRecorder 保持停止）
 *  音量回调 onUserVoiceVolume → onRms 归一化 / onError 进房后 → DISCONNECTED
 */
class RtcClientTest {

    private lateinit var engine: TRTCCloud
    private lateinit var listener: TRTCCloudListener
    private val listeners = mutableListOf<TRTCCloudListener>()
    private lateinit var ctx: Context
    private lateinit var client: RtcClient

    private var addListenerCount = 0
    private var removeListenerCount = 0
    private var enterRoomCount = 0
    private var startLocalAudioCount = 0
    private var exitRoomCount = 0
    private var stopLocalAudioCount = 0
    private var clearAudioFrameListenerCount = 0
    private var localAudioActive = false
    private var muted: Boolean? = null
    private var exitedCount = 0
    private var engineFactoryCount = 0
    private var destroyEngineCount = 0
    private var releaseClaimed = CountDownLatch(0)

    private data class ScheduledDelay(val ms: Long, val callback: () -> Unit, var cancelled: Boolean = false)
    private val scheduledDelays = mutableListOf<ScheduledDelay>()
    private val enteredGenerations = mutableListOf<Long>()
    private val failedGenerations = mutableListOf<Long>()
    private val states = mutableListOf<ConnectionState>()
    private val phases = mutableListOf<VoicePhase>()
    private val errors = mutableListOf<Pair<String, String>>()
    private val rmsValues = mutableListOf<Float>()

    private fun makeSession() = VoiceSessionApi.VoiceSession(
        roomId = "jax-test-device",
        userId = "test-device",
        userSig = "fake-user-sig",
        sdkAppId = 1600155678,
        scene = "audio_call"
    )

    @Before
    fun setUp() {
        VoiceController.reset()
        addListenerCount = 0
        removeListenerCount = 0
        enterRoomCount = 0
        startLocalAudioCount = 0
        exitRoomCount = 0
        stopLocalAudioCount = 0
        clearAudioFrameListenerCount = 0
        localAudioActive = false
        muted = null
        exitedCount = 0
        engineFactoryCount = 0
        destroyEngineCount = 0
        releaseClaimed = CountDownLatch(0)
        scheduledDelays.clear()
        enteredGenerations.clear(); failedGenerations.clear(); listeners.clear()
        states.clear(); phases.clear(); errors.clear(); rmsValues.clear()

        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers {
            addListenerCount++
            listener = arg(0)
            listeners.add(listener)
        }
        every { engine.removeListener(any()) } answers { removeListenerCount++ }
        every { engine.enterRoom(any(), any()) } answers { enterRoomCount++ }
        every { engine.startLocalAudio(any()) } answers {
            startLocalAudioCount++
            localAudioActive = true
        }
        every { engine.exitRoom() } answers { exitRoomCount++ }
        every { engine.stopLocalAudio() } answers {
            stopLocalAudioCount++
            localAudioActive = false
        }
        every { engine.setAudioFrameListener(null) } answers { clearAudioFrameListenerCount++ }
        every { engine.muteLocalAudio(any()) } answers { muted = firstArg() }

        ctx = mockk<Context>(relaxed = true)
        client = RtcClient(
            appContext = ctx,
            onState = { states.add(it) },
            onPhase = { phases.add(it) },
            onRms = { rmsValues.add(it) },
            onError = { code, msg -> errors.add(code to msg) },
            onEntered = { generation -> enteredGenerations.add(generation) },
            onSessionFailure = { generation, _, _ -> failedGenerations.add(generation) },
            onExited = { exitedCount++ },
            engineFactory = {
                engineFactoryCount++
                engine
            },
            destroyEngine = { destroyEngineCount++ },
            onReleaseClaimed = { releaseClaimed.countDown() },
            delayScheduler = { ms, callback ->
                val scheduled = ScheduledDelay(ms, callback)
                scheduledDelays.add(scheduled)
                RtcClient.DelayHandle { scheduled.cancelled = true }
            }
        )
    }

    // ---- 回调触发点（按 SDK 真实回调名接线，javap 核验通过）----
    private fun Thread.joinAndCheckTerminated(timeoutMs: Long = 2_000): Boolean {
        join(timeoutMs)
        return !isAlive
    }

    private fun fireOnEnterRoom(result: Long) = listeners.last().onEnterRoom(result)
    private fun fireOnExitRoom(reason: Int) = listeners.first().onExitRoom(reason)
    private fun fireOnConnectionLost() = listeners.first().onConnectionLost()
    private fun fireOnTryToReconnect() = listeners.first().onTryToReconnect()
    private fun fireOnConnectionRecovery() = listeners.first().onConnectionRecovery()
    private fun fireOnUserVoiceVolume(total: Int) =
        listeners.first().onUserVoiceVolume(arrayListOf<TRTCCloudDef.TRTCVolumeInfo>(), total)
    private fun fireOnError(code: Int, msg: String) = listeners.last().onError(code, msg, null)
    private fun fireDelay(ms: Long) {
        val scheduled = scheduledDelays.firstOrNull { it.ms == ms && !it.cancelled }
            ?: throw AssertionError("no active delay scheduled for ${ms}ms")
        scheduled.cancelled = true
        scheduled.callback()
    }

    // ---- S1: 进房成功 -> CONNECTED + startLocalAudio ----
    @Test
    fun `wake then enter room success CONNECTED and startLocalAudio called`() {
        client.enterRoom(makeSession())
        // 进房前 = CONNECTING
        assertEquals(ConnectionState.CONNECTING, states.last())
        fireOnEnterRoom(0)
        assertEquals(ConnectionState.CONNECTED, states.last())
        assertEquals("startLocalAudio(SPEECH) 应被调用一次", 1, startLocalAudioCount)
        assertEquals("应只进房一次", 1, enterRoomCount)
        assertTrue(client.isInRoom())
        // mic handoff：会话期不触发 onExited（MicRecorder 保持停止）
        assertEquals("会话期不得触发 onExited（mic 由 TRTC 独占）", 0, exitedCount)
    }

    @Test
    fun `SDK enter callbacks preserve attempt generation`() {
        client.enterRoom(makeSession(), generation = 41)
        fireOnEnterRoom(0)
        assertEquals(listOf(41L), enteredGenerations)
        client.exitRoom()
        fireOnExitRoom(0)

        client.enterRoom(makeSession(), generation = 42)
        fireOnEnterRoom(-3316)
        assertEquals(listOf(42L), failedGenerations)
        assertEquals(listOf(41L), enteredGenerations)
    }

    // ---- S2: 进房失败 -> 错误态 + 非 CONNECTED ----
    @Test
    fun `enter room fail not CONNECTED and error set`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(-3316)
        assertNotEquals(ConnectionState.CONNECTED, states.last())
        assertEquals(ConnectionState.DISCONNECTED, states.last())
        assertFalse(client.isInRoom())
        assertTrue("进房失败应上报错误", errors.any { it.first == "enter_room" })
    }

    @Test
    fun `enter failure followed by lifecycle exit releases local audio`() {
        client.enterRoom(makeSession(), generation = 7)
        assertTrue("进房尝试已启动本地采集", localAudioActive)

        fireOnEnterRoom(-3316)
        client.exitRoom()
        fireOnExitRoom(0)
        fireOnExitRoom(0)

        assertFalse("进房失败后的退出清理不得残留 TRTC 本地音频采集", localAudioActive)
        assertEquals("进房失败 teardown 必须显式停止本地音频一次", 1, stopLocalAudioCount)
        assertEquals("进房失败 teardown 必须清理本地帧监听一次", 1, clearAudioFrameListenerCount)
        assertEquals("进房失败 teardown 必须向 SDK 退房一次", 1, exitRoomCount)
        assertEquals("真实退房与重复 lifecycle exit 只能完成一次", 1, exitedCount)
        assertEquals(ConnectionState.DISCONNECTED, states.last())
    }

    @Test
    fun `release tears down pending enter once`() {
        client.enterRoom(makeSession(), generation = 9)

        client.release()
        client.release()

        assertFalse(localAudioActive)
        assertEquals(1, stopLocalAudioCount)
        assertEquals(1, clearAudioFrameListenerCount)
        assertEquals(1, exitRoomCount)
        assertEquals("destroy/release 不得通过 onExited 重启 service mic", 0, exitedCount)
    }

    @Test
    fun `release waits for enter SDK call and prevents later local audio`() {
        val enterStarted = CountDownLatch(1)
        val allowEnter = CountDownLatch(1)
        releaseClaimed = CountDownLatch(1)
        every { engine.enterRoom(any(), any()) } answers {
            enterRoomCount++
            enterStarted.countDown()
            assertTrue("test must release while enterRoom is blocked", allowEnter.await(2, TimeUnit.SECONDS))
        }

        val worker = Thread { client.enterRoom(makeSession(), generation = 63) }
        worker.start()
        assertTrue(enterStarted.await(2, TimeUnit.SECONDS))
        val releaseWorker = Thread { client.release() }
        releaseWorker.start()
        assertTrue("release must claim ownership before waiting for SDK lock", releaseClaimed.await(2, TimeUnit.SECONDS))
        assertEquals("release must wait for enterRoom SDK operation", 0, destroyEngineCount)

        allowEnter.countDown()
        assertTrue("enter worker must terminate", worker.joinAndCheckTerminated())
        assertTrue("release worker must terminate", releaseWorker.joinAndCheckTerminated())

        assertEquals("in-flight enterRoom is called exactly once", 1, enterRoomCount)
        assertEquals("release must prevent local audio after enterRoom", 0, startLocalAudioCount)
        assertEquals("engine must be destroyed exactly once", 1, destroyEngineCount)
        assertEquals("no listener may remain after release", 1, removeListenerCount)
        assertFalse(client.hasActiveAttempt())
    }

    @Test
    fun `release during listener attach prevents enterRoom and removes listener once`() {
        val attachStarted = CountDownLatch(1)
        val allowAttach = CountDownLatch(1)
        val releaseFinished = CountDownLatch(1)
        releaseClaimed = CountDownLatch(1)
        every { engine.addListener(any()) } answers {
            addListenerCount++
            listener = arg(0)
            listeners.add(listener)
            attachStarted.countDown()
            assertTrue("test must release while addListener is blocked", allowAttach.await(2, TimeUnit.SECONDS))
        }

        val worker = Thread { client.enterRoom(makeSession(), generation = 64) }
        worker.start()
        assertTrue(attachStarted.await(2, TimeUnit.SECONDS))
        val releaseWorker = Thread {
            client.release()
            releaseFinished.countDown()
        }
        releaseWorker.start()
        assertTrue("release must claim ownership before waiting for SDK lock", releaseClaimed.await(2, TimeUnit.SECONDS))
        assertEquals("listener attach must be the only SDK operation in flight", 1, addListenerCount)
        assertEquals("release cannot destroy while attach is blocked", 0, destroyEngineCount)
        assertEquals("release cannot remove the listener before attach completes", 0, removeListenerCount)

        allowAttach.countDown()
        assertTrue("release must finish after attach is released", releaseFinished.await(2, TimeUnit.SECONDS))
        assertTrue("enter worker must terminate", worker.joinAndCheckTerminated())
        assertTrue("release worker must terminate", releaseWorker.joinAndCheckTerminated())

        assertEquals("release race must not reach enterRoom", 0, enterRoomCount)
        assertEquals("release race must not start local audio", 0, startLocalAudioCount)
        assertEquals("attached listener must be removed exactly once", 1, removeListenerCount)
        assertEquals("engine must be destroyed exactly once", 1, destroyEngineCount)
        assertFalse(client.hasActiveAttempt())
    }

    @Test
    fun `late enter callback from previous attempt keeps original generation`() {
        client.enterRoom(makeSession(), generation = 61)
        val firstAttemptListener = listeners.last()
        client.exitRoom()
        fireOnExitRoom(0)

        client.enterRoom(makeSession(), generation = 62)
        firstAttemptListener.onEnterRoom(0)

        assertTrue("旧 attempt 成功回调不得被贴到新 generation", enteredGenerations.isEmpty())
        assertFalse(client.isInRoom())
        fireOnEnterRoom(0)
        assertEquals(listOf(62L), enteredGenerations)
    }

    @Test
    fun `late exit and error callbacks from previous attempt cannot mutate new attempt`() {
        client.enterRoom(makeSession(), generation = 71)
        val firstAttemptListener = listeners.last()
        client.exitRoom()
        firstAttemptListener.onExitRoom(0)

        client.enterRoom(makeSession(), generation = 72)
        val stateCount = states.size
        val errorCount = errors.size
        firstAttemptListener.onExitRoom(2)
        firstAttemptListener.onError(-3301, "late failure", null)

        assertEquals("旧 attempt 回调不得结束新 attempt", stateCount, states.size)
        assertEquals("旧 attempt 错误不得污染新 attempt", errorCount, errors.size)
        assertTrue(client.hasActiveAttempt())
        listeners.last().onEnterRoom(0)
        assertEquals(listOf(72L), enteredGenerations)
    }

    @Test
    fun `enter success wins over already captured timeout callback`() {
        client.enterRoom(makeSession(), generation = 81)
        val timeout = scheduledDelays.first { it.ms == 15_000L }

        listeners.last().onEnterRoom(0)
        timeout.callback()

        assertEquals(listOf(81L), enteredGenerations)
        assertTrue(failedGenerations.isEmpty())
        assertFalse(errors.any { it.first == "enter_timeout" })
        assertTrue(client.isInRoom())
    }

    @Test
    fun `enter timeout wins over late success callback`() {
        client.enterRoom(makeSession(), generation = 82)
        val currentListener = listeners.last()

        fireDelay(15_000)
        currentListener.onEnterRoom(0)

        assertEquals(listOf(82L), failedGenerations)
        assertTrue(enteredGenerations.isEmpty())
        assertFalse(client.isInRoom())
        assertEquals(1, exitRoomCount)
    }

    @Test
    fun `synchronous enter failure cannot start local audio after teardown`() {
        every { engine.enterRoom(any(), any()) } answers {
            enterRoomCount++
            listeners.last().onEnterRoom(-3316)
        }

        client.enterRoom(makeSession(), generation = 90)

        assertEquals(listOf(90L), failedGenerations)
        assertFalse(client.hasActiveAttempt())
        assertEquals(0, startLocalAudioCount)
        assertFalse(localAudioActive)
        assertEquals(1, exitRoomCount)
    }

    @Test
    fun `engine initialization failure rolls attempt back and reports generation`() {
        client = RtcClient(
            appContext = ctx,
            onState = { states.add(it) },
            onPhase = { phases.add(it) },
            onRms = { rmsValues.add(it) },
            onError = { code, msg -> errors.add(code to msg) },
            onEntered = { generation -> enteredGenerations.add(generation) },
            onSessionFailure = { generation, _, _ -> failedGenerations.add(generation) },
            onExited = { exitedCount++ },
            engineFactory = { throw IllegalStateException("engine unavailable") },
            destroyEngine = { destroyEngineCount++ },
            delayScheduler = { _, _ -> RtcClient.DelayHandle {} }
        )

        client.enterRoom(makeSession(), generation = 91)

        assertFalse(client.hasActiveAttempt())
        assertEquals(listOf(91L), failedGenerations)
        assertTrue(errors.any { it.first == "engine_init" })
        assertEquals(ConnectionState.DISCONNECTED, states.last())
    }

    @Test
    fun `release before first enter does not initialize cloud and controls remain inert`() {
        client.release()
        client.release()
        client.enterRoom(makeSession(), generation = 101)
        client.exitRoom()
        client.muteLocal(true)
        client.interruptRemotePlayback()

        assertEquals(0, engineFactoryCount)
        assertEquals(0, destroyEngineCount)
        assertEquals(0, enterRoomCount)
        assertFalse(client.hasActiveAttempt())
    }

    @Test
    fun `enter room idempotent while callback is pending`() {
        client.enterRoom(makeSession(), generation = 12)
        client.enterRoom(makeSession(), generation = 13)

        assertEquals(1, enterRoomCount)
        assertEquals(1, startLocalAudioCount)
        fireOnEnterRoom(0)
        assertEquals(listOf(12L), enteredGenerations)
    }

    // ---- S3: 重复进房幂等（QA-PLAN §2 场景 C）----
    @Test
    fun `enter room idempotent when already connected`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        val roomsBefore = enterRoomCount
        client.enterRoom(makeSession()) // 已 CONNECTED 再次进房
        assertEquals("不应重复进房", roomsBefore, enterRoomCount)
        assertEquals(ConnectionState.CONNECTED, states.last())
    }

    // ---- R1: 断线 -> 重连回调链 -> CONNECTED，不丢会话上下文 ----
    @Test
    fun `connection lost then reconnect then recovery CONNECTED and no IDLE reset`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        fireOnConnectionLost()
        assertEquals(ConnectionState.CONNECTING, states.last())
        fireOnTryToReconnect()
        assertEquals(ConnectionState.CONNECTING, states.last())
        fireOnConnectionRecovery()
        assertEquals(ConnectionState.CONNECTED, states.last())
        // 会话上下文不丢：不进 IDLE / 不触发退房
        assertFalse("重连期间不得 reset 到 IDLE", phases.contains(VoicePhase.IDLE))
        assertEquals("重连不得误判退房", 0, exitedCount)
    }

    // ---- R2: 重连失败保持重试（不误判退出房间）----
    @Test
    fun `reconnect keeps retrying without recovery stays CONNECTING`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        fireOnConnectionLost()
        repeat(3) { fireOnTryToReconnect() } // SDK 每 24s 重试
        assertEquals(ConnectionState.CONNECTING, states.last())
        assertEquals("重连失败不得触发退房", 0, exitedCount)
        assertEquals("SDK 重连期间不应主动 exitRoom", 0, exitRoomCount)
    }

    // ---- E1: 正常退房 -> 等 onExitRoom -> onExited（MicRecorder 恢复点）----
    @Test
    fun `exit room waits onExitRoom before onExited`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        client.exitRoom()
        assertEquals("exitRoom 应被调用一次", 1, exitRoomCount)
        assertEquals("未收到 onExitRoom 前不得触发 onExited", 0, exitedCount)
        fireOnExitRoom(0)
        assertEquals("onExitRoom 后才触发 onExited（mic handoff 恢复）", 1, exitedCount)
        assertEquals(ConnectionState.DISCONNECTED, states.last())
        assertFalse(client.isInRoom())
    }

    // ---- E2: 只 exitRoom 无 onExitRoom -> onExited 不触发（防 mic 抢占竞态，ADR-012 实施补充）----
    @Test
    fun `exitRoom without onExitRoom callback does not restart mic`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        client.exitRoom()
        // 模拟 onExitRoom 迟迟不来
        assertEquals("无 onExitRoom 不得重启 MicRecorder", 0, exitedCount)
    }

    @Test
    fun `enter timeout followed by exit completes onExited exactly once`() {
        client.enterRoom(makeSession(), generation = 51)
        fireDelay(15_000)
        assertTrue(errors.any { it.first == "enter_timeout" })
        assertEquals(listOf(51L), failedGenerations)
        client.exitRoom()
        fireOnExitRoom(0)
        fireOnExitRoom(0)

        assertEquals("enter timeout 后真实退房只能完成一次", 1, exitedCount)
        assertEquals(1, stopLocalAudioCount)
        assertEquals(1, exitRoomCount)
    }

    @Test
    fun `enter timeout followed by missing exit callback uses fallback exactly once`() {
        client.enterRoom(makeSession(), generation = 52)
        fireDelay(15_000)
        client.exitRoom()
        fireDelay(3_000)

        assertEquals("enter timeout 后退房兜底必须完成一次", 1, exitedCount)
        assertEquals(1, stopLocalAudioCount)
        assertEquals(1, exitRoomCount)
    }

    // ---- 音量回调: totalVolume 0~100 -> rms 0~1 ----
    @Test
    fun `user voice volume maps to normalized rms`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        fireOnUserVoiceVolume(55)
        assertEquals(0.55f, rmsValues.last(), 0.001f)
        fireOnUserVoiceVolume(100)
        assertEquals(1.0f, rmsValues.last(), 0.001f)
    }

    // ---- onError 进房后 -> DISCONNECTED + 错误上报 ----
    @Test
    fun `onError while in room DISCONNECTED and error reported`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        fireOnError(-3317, "room error")
        assertEquals(ConnectionState.DISCONNECTED, states.last())
        assertTrue(errors.any { it.first == "-3317" })
    }

    // ---- 静音控制转发 ----
    @Test
    fun `muteLocal forwards to engine`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        client.muteLocal(true)
        assertEquals(true, muted)
        client.muteLocal(false)
        assertEquals(false, muted)
    }
}
