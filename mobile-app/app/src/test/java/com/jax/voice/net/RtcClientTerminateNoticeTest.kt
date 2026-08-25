package com.jax.voice.net

import android.content.Context
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoicePhase
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudListener
import io.mockk.every
import io.mockk.mockk
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * RtcClient 终止通知 L0 单测（Task #23 上游接线，TDD）。
 *
 * 覆盖：sendTerminationNotice 成功（payload/cmdId/reliable/ordered 契约）、SDK false、
 * SDK 抛异常 fail-safe、空 tid 拦截、onRecvCustomCmdMsg 反向命令预留不崩。
 * 仿 RtcClientTest 的 engineFactory 注入模式，mock TRTC SDK 不连真实云。
 */
class RtcClientTerminateNoticeTest {

    private lateinit var engine: TRTCCloud
    private lateinit var listener: TRTCCloudListener
    private lateinit var ctx: Context
    private lateinit var client: RtcClient

    private val sentPayloads = mutableListOf<ByteArray>()
    private val sentCmdIds = mutableListOf<Int>()
    private val sentReliable = mutableListOf<Boolean>()
    private val sentOrdered = mutableListOf<Boolean>()
    private var sdkResult = true

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
        sentPayloads.clear(); sentCmdIds.clear()
        sentReliable.clear(); sentOrdered.clear()
        sdkResult = true

        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers { listener = arg(0) }
        every { engine.enterRoom(any(), any()) } answers { }
        every { engine.startLocalAudio(any()) } answers { }
        every {
            engine.sendCustomCmdMsg(any(), any(), any(), any())
        } answers {
            // javap 核对 13.4.0.20477：sendCustomCmdMsg(int cmdId, byte[] data, boolean reliable, boolean ordered)
            sentCmdIds += firstArg<Int>()
            sentPayloads += secondArg<ByteArray>()
            sentReliable += thirdArg<Boolean>()
            sentOrdered += arg<Boolean>(3)
            sdkResult
        }

        ctx = mockk<Context>(relaxed = true)
        client = RtcClient(
            appContext = ctx,
            onState = { },
            onPhase = { },
            onRms = { },
            onError = { _, _ -> },
            onExited = { },
            onEntered = { },
            engineFactory = { engine }
        )
        client.enterRoom(makeSession())
        listener.onEnterRoom(0) // 进房成功 → isInRoom
    }

    @Test
    fun `termination notice sends cmdId1 reliable ordered payload with tid`() {
        assertTrue(client.sendTerminationNotice("tid-9"))
        assertEquals("应只发一次", 1, sentPayloads.size)
        assertEquals(RtcClient.CMD_ID_TERMINATE, sentCmdIds.single())
        assertTrue("终止通知必须 reliable", sentReliable.single())
        assertTrue("终止通知必须 ordered", sentOrdered.single())
        val json = JSONObject(String(sentPayloads.single(), Charsets.UTF_8))
        assertEquals("note_termination", json.getString("type"))
        assertEquals("tid-9", json.getString("termination_id"))
    }

    @Test
    fun `termination notice returns false when sdk reports failure`() {
        sdkResult = false
        assertFalse(client.sendTerminationNotice("tid-9"))
        assertEquals("失败也应有发送尝试（供上层重试决策）", 1, sentPayloads.size)
    }

    @Test
    fun `termination notice is fail-safe when sdk throws`() {
        every { engine.sendCustomCmdMsg(any(), any(), any(), any()) } throws
            IllegalStateException("engine released")
        assertFalse("SDK 异常不得上抛", client.sendTerminationNotice("tid-9"))
    }

    @Test
    fun `termination notice rejects blank tid without touching sdk`() {
        assertFalse(client.sendTerminationNotice(""))
        assertFalse(client.sendTerminationNotice("   "))
        assertEquals(0, sentPayloads.size)
    }

    @Test
    fun `onRecvCustomCmdMsg reverse channel stub does not crash`() {
        // 预留的反向命令通道：收到任何消息只记录不处理，绝不能抛错影响通话主链路
        listener.onRecvCustomCmdMsg("jax-pc-sidecar", 1, 0, byteArrayOf(1, 2, 3))
        listener.onRecvCustomCmdMsg("jax-pc-sidecar", 99, 1, null)
    }
}
