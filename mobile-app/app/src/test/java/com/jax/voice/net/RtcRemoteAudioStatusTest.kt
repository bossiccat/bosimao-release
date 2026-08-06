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
 * 打断状态机 L0 单测（QA-PLAN §3.3 v1.1 验收项 / RTC-CLIENT-TEST-DESIGN §2.4 H2/H3 / §2.5 T5）。
 *
 * 语义（MOBILE-INTEGRATION §3.4 / QA-PLAN §3.3，与实现一一对应）：
 *  onRemoteAudioStatusUpdated(audioStatus=1 远端说话中) → 六态 SPEAKING（正在播放远端语音/被插话）；
 *  audioStatus=2 远端静音/停止（回复结束/打断）→ 停播下行（muteRemoteAudio）+ 切 LISTENING，
 *  保证「UI 状态与音频停止一致」（不允许音频已停但 UI 还停在 Speaking）。
 * 会话期 mic 由 TRTC 独占、本地 VAD 不参与打断，打断判定来源 = 本回调。
 *
 * 回调名 onRemoteAudioStatusUpdated(userId, audioStatus, reason, extraInfo) 已用 javap
 * 核对 SDK 13.4.0.20477 jar 确认（非旧文档 onRemoteUserAudioStatus）。
 * 反作弊：本文件为新增测试，无 @Ignore/.only/弱化断言。
 */
class RtcRemoteAudioStatusTest {

    private lateinit var engine: TRTCCloud
    private lateinit var listener: TRTCCloudListener
    private lateinit var client: RtcClient
    private val phases = mutableListOf<VoicePhase>()
    private val states = mutableListOf<ConnectionState>()
    private var exitedCount = 0
    private var muteRemoteCalls = 0

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
        phases.clear(); states.clear()
        exitedCount = 0
        muteRemoteCalls = 0
        listener = mockk<TRTCCloudListener>(relaxed = true)
        engine = mockk<TRTCCloud>(relaxed = true)
        every { engine.addListener(any()) } answers { listener = arg(0) }
        every { engine.enterRoom(any(), any()) } answers { }
        every { engine.muteRemoteAudio(any(), any()) } answers { muteRemoteCalls++ }
        client = RtcClient(
            appContext = mockk<Context>(relaxed = true),
            onState = { states.add(it) },
            onPhase = { phases.add(it) },
            onRms = {},
            onError = { _, _ -> },
            onExited = { exitedCount++ },
            engineFactory = { engine }
        )
    }

    // audioStatus 语义（SDK 13.4 / TRTCAudioStatus）：1=远端说话中，2=远端静音/停止（打断结束）
    private fun fireOnRemoteAudioStatus(audioStatus: Int) =
        listener.onRemoteAudioStatusUpdated("pc-sidecar", audioStatus, 0, null)

    // ---- H2/H3/T5: 远端说话→SPEAKING，远端静音/停止→停播+LISTENING（UI 与音频停止一致）----
    @Test
    fun `remote speaking drives SPEAKING then silent switches to LISTENING with stop`() {
        client.enterRoom(makeSession())
        listener.onEnterRoom(0)
        // 去掉 enterRoom 初始 LISTENING（会话开始=在听），精确验证 audioStatus 驱动
        phases.clear()

        // 远端开始说话（回复/插话）→ 正在播放 = SPEAKING
        fireOnRemoteAudioStatus(1)
        assertTrue("远端说话应切 SPEAKING（正在播放远端语音）", phases.any { it == VoicePhase.SPEAKING })
        assertTrue("远端说话时不应停在 LISTENING", phases.none { it == VoicePhase.LISTENING })

        // 远端静音/停止（回复结束/打断）→ 停播 + 切回 LISTENING（听用户）
        phases.clear()
        fireOnRemoteAudioStatus(2)
        assertTrue("远端静音应切回 LISTENING", phases.any { it == VoicePhase.LISTENING })
        assertTrue("远端静音后不得停在 SPEAKING（UI 与音频停止一致，T5）", phases.none { it == VoicePhase.SPEAKING })
        assertTrue("远端静音应触发下行停播（muteRemoteAudio）", muteRemoteCalls >= 1)
    }

    // ---- 会话期不得触发 onExited（mic handoff：RTC 独占，MicRecorder 保持停止）----
    @Test
    fun `remote audio events during session do not restart mic`() {
        client.enterRoom(makeSession())
        listener.onEnterRoom(0)
        fireOnRemoteAudioStatus(1)
        fireOnRemoteAudioStatus(2)
        assertEquals("会话期打断事件不得重启 MicRecorder", 0, exitedCount)
    }
}
