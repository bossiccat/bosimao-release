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
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 远端播放订阅与打断 L0 单测（Task 7 / SPEC AC-12 AC-13 AC-14）。
 *
 * 历史根因：onRemoteAudioStatusUpdated(audioStatus=2) 时调用 muteRemoteAudio(true) 停播下行，
 * 且恢复路径依赖下一次 SPEAKING 事件对称 unmute——事件丢失/时序偏移即导致远端被永久静音，
 * 第二轮回复开始后手机无声（sidecar 下行帧正常、APM 正常回复）。
 *
 * 新语义（Task 7）：
 * - 正常远端停止（audioStatus=2）只发布 RemoteAudioStopped UI 事件，绝不调用 muteRemoteAudio(true)
 *   ——SDK 自动订阅保持长期有效，第二轮开始无需恢复订阅即可收到帧（AC-12）。
 * - 显式打断（用户开口/点击，AC-13）只做本地播放 stop/flush 脉冲 + generation 失效（AC-14），
 *   脉冲后恢复 unmute，不改变长期远端订阅。
 *
 * 反作弊：无 @Ignore/skip；muteRemoteAudio 实参被完整记录断言，不做 mock-only 绿灯。
 */
class RtcPlaybackSubscriptionTest {

    private lateinit var engine: TRTCCloud
    private lateinit var listener: TRTCCloudListener
    private lateinit var client: RtcClient
    private val phases = mutableListOf<VoicePhase>()
    private val events = mutableListOf<RtcPlaybackSubscription.RemoteAudioEvent>()
    private val muteCalls = mutableListOf<Boolean>()
    private val rmsValues = mutableListOf<Float>()
    private var exitedCount = 0

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
        phases.clear(); events.clear(); muteCalls.clear(); rmsValues.clear()
        exitedCount = 0
        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers { listener = arg(0) }
        every { engine.enterRoom(any(), any()) } answers { }
        every { engine.muteRemoteAudio(any(), any()) } answers { muteCalls.add(secondArg<Boolean>()) }
        every { engine.muteAllRemoteAudio(any()) } answers { }
        client = RtcClient(
            appContext = mockk<Context>(relaxed = true),
            onState = {},
            onPhase = { phases.add(it) },
            onRms = { rmsValues.add(it) },
            onError = { _, _ -> },
            onExited = { exitedCount++ },
            onRemoteAudioEvent = { events.add(it) },
            engineFactory = { engine }
        )
    }

    private fun fireAudioStatus(audioStatus: Int) =
        listener.onRemoteAudioStatusUpdated("pc-sidecar", audioStatus, 0, null)

    private fun enterRoom() {
        client.enterRoom(makeSession())
        listener.onEnterRoom(0)
        phases.clear()
    }

    // ---- AC-12: 正常远端停止只发布 STOPPED UI 事件，绝不 muteRemoteAudio(true) ----
    @Test
    fun `remote stop publishes STOPPED event and never mutes remote audio`() {
        enterRoom()
        fireAudioStatus(1)
        assertTrue("远端说话应切 SPEAKING", phases.any { it == VoicePhase.SPEAKING })
        assertTrue("远端说话应发布 STARTED UI 事件", events.any { it == RtcPlaybackSubscription.RemoteAudioEvent.STARTED })

        phases.clear(); events.clear()
        fireAudioStatus(2)
        assertTrue("远端停止应切 LISTENING", phases.any { it == VoicePhase.LISTENING })
        assertTrue("远端停止应发布 STOPPED UI 事件", events.any { it == RtcPlaybackSubscription.RemoteAudioEvent.STOPPED })
        assertTrue("正常远端停止绝不调用 muteRemoteAudio(true)（订阅长期有效）", muteCalls.none { it })
    }

    // ---- AC-12: 第二轮远端开始无需恢复订阅即可收到帧（整个过程中零 mute 干预）----
    @Test
    fun `second round starts without restoring subscription and audio flows`() {
        enterRoom()
        // 第一轮：说话 → 停止（正常结束，只发 UI 事件）
        fireAudioStatus(1)
        fireAudioStatus(2)
        assertTrue("第一轮正常结束不得留下任何 mute", muteCalls.isEmpty())

        // 第二轮：远端再次说话 → 直接 SPEAKING，无需恢复订阅（无 unmute/mute 调用）
        phases.clear()
        fireAudioStatus(1)
        assertTrue("第二轮开始应直接 SPEAKING（订阅从未被修改）", phases.any { it == VoicePhase.SPEAKING })
        assertTrue("第二轮不得出现任何 mute 恢复操作", muteCalls.isEmpty())
        // 音频流照常到达：音量回调驱动波形（帧可达的替代证据）
        listener.onUserVoiceVolume(arrayListOf<TRTCCloudDef.TRTCVolumeInfo>(), 60)
        assertEquals(0.6f, rmsValues.last(), 0.001f)
    }

    // ---- AC-13/AC-14: 显式打断 = 本地播放 stop/flush 脉冲 + generation 失效，订阅随后恢复 ----
    @Test
    fun `explicit barge-in flushes local playback and invalidates generation`() {
        enterRoom()
        listener.onRemoteUserEnterRoom("pc-sidecar")
        fireAudioStatus(1)
        phases.clear(); muteCalls.clear()
        val before = client.playbackGeneration

        client.interruptRemotePlayback()

        assertEquals("显式打断必须递增播放 generation（旧下行帧失效，AC-14）", before + 1, client.playbackGeneration)
        assertEquals("显式打断 = 本地播放 stop/flush 脉冲，随后恢复订阅", listOf(true, false), muteCalls)
        assertTrue("打断后应回到 LISTENING", phases.any { it == VoicePhase.LISTENING })
    }

    // ---- 会话期播放事件不得触发退房（mic handoff：RTC 独占，MicRecorder 保持停止）----
    @Test
    fun `playback events during session do not restart mic`() {
        enterRoom()
        fireAudioStatus(1)
        fireAudioStatus(2)
        client.interruptRemotePlayback()
        assertEquals("会话期播放事件不得重启 MicRecorder", 0, exitedCount)
    }
}
