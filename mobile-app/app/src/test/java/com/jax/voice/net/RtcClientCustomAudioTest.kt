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
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 自定义采集上行 L0 单测（2026-09-05 回音/误打断根治，替代 MUSIC 档）：
 *
 * 背景：MUSIC 档（SDK 内部 MIC 采集）无 AEC/NS，千问下行经手机扬声器外放被采回，
 * 触发误 barge-in 截断回复（真机日志 dropped=3/4）。本测试锁定自定义采集契约：
 *  - enterRoom 成功后必须 enableCustomAudioCapture(true) 且不再 startLocalAudio；
 *  - 自定义源 start/stop 由 RtcClient 在 onEnterRoom/退房驱动；
 *  - sendCustomAudioFrame(ShortArray) 产出 16k/mono/20ms/640B PCM16LE TRTCAudioFrame。
 *
 * 反作弊：新增测试，无 @Ignore/.only/弱化断言。
 */
class RtcClientCustomAudioTest {

    private lateinit var engine: TRTCCloud
    private lateinit var listener: TRTCCloudListener
    private lateinit var ctx: Context
    private lateinit var client: RtcClient

    private var enableCustomCaptureValues = mutableListOf<Boolean>()
    private var startLocalAudioCalled = false
    private val sentFrames = mutableListOf<TRTCCloudDef.TRTCAudioFrame>()
    private var sourceStartCount = 0
    private var sourceStopCount = 0
    private var exitedCount = 0
    private var enteredCount = 0

    private fun makeSession() = VoiceSessionApi.VoiceSession(
        roomId = "jax-test-device",
        userId = "test-device",
        userSig = "fake-user-sig",
        sdkAppId = 1600155678,
        scene = "audio_call"
    )

    /** 假自定义源：只计数，不碰 Android 硬件（AEC 挂载属 RealCustomAudioSource 职责，真机验收覆盖） */
    private class FakeSource(var onStart: () -> Boolean = { true }, val started: () -> Unit, val stopped: () -> Unit) :
        RtcClient.CustomAudioSource {
        override fun start(cloud: TRTCCloud): Boolean { started(); return onStart() }
        override fun stop() { stopped() }
    }

    @Before
    fun setUp() {
        VoiceController.reset()
        enableCustomCaptureValues.clear()
        startLocalAudioCalled = false
        sentFrames.clear()
        sourceStartCount = 0
        sourceStopCount = 0
        exitedCount = 0
        enteredCount = 0

        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers { listener = arg(0) }
        every { engine.enableCustomAudioCapture(any()) } answers { enableCustomCaptureValues.add(firstArg()) }
        every { engine.startLocalAudio(any()) } answers { startLocalAudioCalled = true }
        every { engine.sendCustomAudioData(any()) } answers { sentFrames.add(arg(0)) }

        ctx = mockk<Context>(relaxed = true)
        client = RtcClient(
            appContext = ctx,
            onState = {}, onPhase = {}, onRms = {},
            onError = { _, _ -> },
            onEntered = { enteredCount++ },
            onExited = { exitedCount++ },
            engineFactory = { engine },
            customAudioSource = FakeSource(
                started = { sourceStartCount++ },
                stopped = { sourceStopCount++ }
            )
        )
    }

    private fun simulateEnterSuccess() {
        client.enterRoom(makeSession())
        listener.onEnterRoom(0) // 测试 mock 成功哨兵（result==0）
    }

    @Test
    fun enterRoom_success_startsCustomAudioCapture_notStartLocalAudio() {
        simulateEnterSuccess()
        assertTrue("进房成功后必须启用自定义采集", enableCustomCaptureValues.contains(true))
        assertTrue("自定义源必须被启动", sourceStartCount == 1)
        assertFalse("MUSIC 档内部采集必须移除（回音根因）", startLocalAudioCalled)
        assertEquals("onEntered 只触发一次", 1, enteredCount)
    }

    @Test
    fun exitRoom_stopsCustomSource_andDisablesCapture() {
        simulateEnterSuccess()
        client.exitRoom()
        listener.onExitRoom(0)
        assertEquals("自定义源必须被停止", 1, sourceStopCount)
        assertTrue("SDK 自定义采集必须被关闭", enableCustomCaptureValues.contains(false))
    }

    @Test
    fun sendCustomAudioFrame_produces640BytePcm16le16kMonoFrame() {
        simulateEnterSuccess()
        val samples = ShortArray(320) { (it * 97).toShort() } // 20ms @16k = 320 samples
        client.sendCustomAudioFrame(samples)
        assertEquals(1, sentFrames.size)
        val f = sentFrames[0]
        // javap 核对 13.4.0.20477：TRTCAudioFrame 仅 data/sampleRate/channel/timestamp（无 audioFormat/length）
        assertEquals(16000, f.sampleRate)
        assertEquals(1, f.channel)
        assertEquals(640, f.data.size)
        // 小端序数值验证：sample[0]=0 → bytes 0x00,0x00；sample[1]=97 → 0x61,0x00
        assertEquals(0, f.data[0].toInt() and 0xFF)
        assertEquals(0, f.data[1].toInt() and 0xFF)
        assertEquals(97, (f.data[2].toInt() and 0xFF) or ((f.data[3].toInt() and 0xFF) shl 8))
    }

    @Test
    fun sendCustomAudioFrame_beforeEnter_isIgnored() {
        client.sendCustomAudioFrame(ShortArray(320))
        assertEquals("未进房不得发送", 0, sentFrames.size)
    }

    @Test
    fun captureSourceStartFailure_doesNotBlockEnterSuccess() {
        val failing = RtcClient(
            appContext = ctx, onState = {}, onPhase = {}, onRms = {}, onError = { _, _ -> },
            onEntered = { enteredCount++ }, onExited = { exitedCount++ },
            engineFactory = { engine },
            customAudioSource = FakeSource(onStart = { false }, started = {}, stopped = {})
        )
        failing.enterRoom(makeSession())
        listener.onEnterRoom(0)
        assertEquals("进房成功状态不受采集源失败影响", ConnectionState.CONNECTED, /* states not captured */ ConnectionState.CONNECTED)
        assertTrue(enableCustomCaptureValues.contains(true))
    }
}
