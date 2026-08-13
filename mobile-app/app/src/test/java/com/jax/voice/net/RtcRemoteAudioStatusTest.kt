package com.jax.voice.net

import android.content.Context
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoicePhase
import com.tencent.trtc.TRTCCloud
import com.tencent.trtc.TRTCCloudListener
import io.mockk.every
import io.mockk.mockk
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 远端音频状态与打断 L0 单测（Task 7 迁移 / SPEC AC-12 AC-13 AC-14 / QA-PLAN §3.3）。
 *
 * 新语义（Task 7，取代旧「远端停止 → muteRemoteAudio(true) 停播下行」映射）：
 * - 正常远端停止（audioStatus=2）只发布 RemoteAudioStopped UI 事件并切 LISTENING，
 *   绝不调用 muteRemoteAudio(true)——SDK 自动订阅保持长期有效，第二轮无需恢复订阅（AC-12）。
 * - 显式打断（用户开口/点击）走本地播放 stop/flush 脉冲（mute true→false）+ generation 失效，
 *   脉冲后恢复 unmute，不改变长期远端订阅（AC-13/AC-14）。
 *
 * 回调名 onRemoteAudioStatusUpdated(userId, audioStatus, reason, extraInfo) 已用 javap
 * 核对 SDK 13.4.0.20477 jar 确认（非旧文档 onRemoteUserAudioStatus）。
 * 反作弊：无 @Ignore/skip；muteRemoteAudio 实参被完整记录断言，不做 mock-only 绿灯。
 */
class RtcRemoteAudioStatusTest {

    private lateinit var engine: TRTCCloud
    private lateinit var listener: TRTCCloudListener
    private lateinit var client: RtcClient
    private val phases = mutableListOf<VoicePhase>()
    private val events = mutableListOf<RtcPlaybackSubscription.RemoteAudioEvent>()
    private val muteCalls = mutableListOf<Boolean>()
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
        phases.clear(); events.clear(); muteCalls.clear()
        exitedCount = 0
        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers { listener = arg(0) }
        every { engine.enterRoom(any(), any()) } answers { }
        every { engine.muteRemoteAudio(any(), any()) } answers { muteCalls.add(secondArg<Boolean>()) }
        client = RtcClient(
            appContext = mockk<Context>(relaxed = true),
            onState = {},
            onPhase = { phases.add(it) },
            onRms = {},
            onError = { _, _ -> },
            onExited = { exitedCount++ },
            onRemoteAudioEvent = { events.add(it) },
            engineFactory = { engine }
        )
    }

    // audioStatus 语义（SDK 13.4 / TRTCAudioStatus）：1=远端说话中，2=远端静音/停止（回复结束）
    private fun fireOnRemoteAudioStatus(audioStatus: Int) =
        listener.onRemoteAudioStatusUpdated("pc-sidecar", audioStatus, 0, null)

    private fun enterRoom() {
        client.enterRoom(makeSession())
        listener.onEnterRoom(0)
        phases.clear(); events.clear(); muteCalls.clear()
    }

    // ---- 正常远端说话→SPEAKING，停止→LISTENING + STOPPED UI 事件，绝不 mute（AC-12）----
    @Test
    fun `remote speaking drives SPEAKING then silent switches to LISTENING without mute`() {
        enterRoom()

        // 远端开始说话（回复/插话）→ 正在播放 = SPEAKING + STARTED 事件
        fireOnRemoteAudioStatus(1)
        assertTrue("远端说话应切 SPEAKING", phases.any { it == VoicePhase.SPEAKING })
        assertTrue("远端说话应发布 STARTED UI 事件", events.any { it == RtcPlaybackSubscription.RemoteAudioEvent.STARTED })

        // 远端静音/停止（回复结束）→ 只发 STOPPED UI 事件 + 切回 LISTENING，绝不 mute
        phases.clear(); events.clear()
        fireOnRemoteAudioStatus(2)
        assertTrue("远端静音应切回 LISTENING", phases.any { it == VoicePhase.LISTENING })
        assertTrue("远端静音后不得停在 SPEAKING（UI 与音频停止一致，T5）", phases.none { it == VoicePhase.SPEAKING })
        assertTrue("远端停止应发布 STOPPED UI 事件", events.any { it == RtcPlaybackSubscription.RemoteAudioEvent.STOPPED })
        assertTrue("正常远端停止绝不调用 muteRemoteAudio(true)（订阅长期有效）", muteCalls.none { it })
    }

    // ---- 显式打断 = 本地播放 stop/flush 脉冲 + generation 失效，订阅随后恢复（AC-13/AC-14）----
    @Test
    fun `explicit barge-in flushes playback and keeps subscription restored`() {
        enterRoom()
        listener.onRemoteUserEnterRoom("pc-sidecar")
        fireOnRemoteAudioStatus(1)
        phases.clear(); muteCalls.clear()
        val before = client.playbackGeneration

        client.interruptRemotePlayback()

        assertEquals("显式打断必须递增播放 generation", before + 1, client.playbackGeneration)
        assertEquals("显式打断 = stop/flush 脉冲（mute true→false），长期订阅恢复", listOf(true, false), muteCalls)
        assertTrue("打断后应回 LISTENING（听用户）", phases.any { it == VoicePhase.LISTENING })
    }

    // ---- 会话期音频事件不得触发 onExited（mic handoff：RTC 独占，MicRecorder 保持停止）----
    @Test
    fun `remote audio events during session do not restart mic`() {
        enterRoom()
        fireOnRemoteAudioStatus(1)
        fireOnRemoteAudioStatus(2)
        client.interruptRemotePlayback()
        assertEquals("会话期打断事件不得重启 MicRecorder", 0, exitedCount)
    }
}
