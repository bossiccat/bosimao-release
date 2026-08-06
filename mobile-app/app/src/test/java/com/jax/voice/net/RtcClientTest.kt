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
    private lateinit var ctx: Context
    private lateinit var client: RtcClient

    private var enterRoomCount = 0
    private var startLocalAudioCalled = false
    private var exitRoomCalled = false
    private var stopLocalAudioCalled = false
    private var muted: Boolean? = null
    private var exitedCount = 0

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
        enterRoomCount = 0
        startLocalAudioCalled = false
        exitRoomCalled = false
        stopLocalAudioCalled = false
        muted = null
        exitedCount = 0
        states.clear(); phases.clear(); errors.clear(); rmsValues.clear()

        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers { listener = arg(0) }
        every { engine.enterRoom(any(), any()) } answers { enterRoomCount++ }
        every { engine.startLocalAudio(any()) } answers { startLocalAudioCalled = true }
        every { engine.exitRoom() } answers { exitRoomCalled = true }
        every { engine.stopLocalAudio() } answers { stopLocalAudioCalled = true }
        every { engine.muteLocalAudio(any()) } answers { muted = firstArg() }

        ctx = mockk<Context>(relaxed = true)
        client = RtcClient(
            appContext = ctx,
            onState = { states.add(it) },
            onPhase = { phases.add(it) },
            onRms = { rmsValues.add(it) },
            onError = { code, msg -> errors.add(code to msg) },
            onExited = { exitedCount++ },
            engineFactory = { engine }
        )
    }

    // ---- 回调触发点（按 SDK 真实回调名接线，javap 核验通过）----
    private fun fireOnEnterRoom(result: Long) = listener.onEnterRoom(result)
    private fun fireOnExitRoom(reason: Int) = listener.onExitRoom(reason)
    private fun fireOnConnectionLost() = listener.onConnectionLost()
    private fun fireOnTryToReconnect() = listener.onTryToReconnect()
    private fun fireOnConnectionRecovery() = listener.onConnectionRecovery()
    private fun fireOnUserVoiceVolume(total: Int) =
        listener.onUserVoiceVolume(arrayListOf<TRTCCloudDef.TRTCVolumeInfo>(), total)
    private fun fireOnError(code: Int, msg: String) = listener.onError(code, msg, null)

    // ---- S1: 进房成功 -> CONNECTED + startLocalAudio ----
    @Test
    fun `wake then enter room success CONNECTED and startLocalAudio called`() {
        client.enterRoom(makeSession())
        // 进房前 = CONNECTING
        assertEquals(ConnectionState.CONNECTING, states.last())
        fireOnEnterRoom(0)
        assertEquals(ConnectionState.CONNECTED, states.last())
        assertTrue("startLocalAudio(SPEECH) 应被调用", startLocalAudioCalled)
        assertEquals("应只进房一次", 1, enterRoomCount)
        assertTrue(client.isInRoom())
        // mic handoff：会话期不触发 onExited（MicRecorder 保持停止）
        assertEquals("会话期不得触发 onExited（mic 由 TRTC 独占）", 0, exitedCount)
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
        assertTrue("SDK 重连期间不应主动 exitRoom", !exitRoomCalled)
    }

    // ---- E1: 正常退房 -> 等 onExitRoom -> onExited（MicRecorder 恢复点）----
    @Test
    fun `exit room waits onExitRoom before onExited`() {
        client.enterRoom(makeSession())
        fireOnEnterRoom(0)
        client.exitRoom()
        assertTrue("exitRoom 应被调用", exitRoomCalled)
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
