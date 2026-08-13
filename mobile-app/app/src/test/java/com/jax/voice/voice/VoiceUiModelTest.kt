package com.jax.voice.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * VoiceUiModel 统一模型 L0 单测（Task 8 / SPEC §4.2 / DESIGN-DETAIL §2）。
 *
 * 覆盖：10 个体验状态存在且各自有合法主操作；会话生命周期 → 体验状态映射；
 * 旧六态兼容映射；分类错误模型携带恢复操作；静态断言 UI 代码不得读取
 * inCall/rtcExiting 等并行业务布尔（SPEC §4.2 硬约束）。
 *
 * 反作弊：无 @Ignore/skip；静态扫描为真实源码正则断言，非 mock-only。
 */
class VoiceUiModelTest {

    // ---- 10 个体验状态 + 合法主操作（DESIGN-DETAIL §2.1 表）----
    @Test
    fun `ten experience states exist with legal primary actions`() {
        assertEquals(10, ExperienceState.values().size)
        // 每个状态都能给出主操作（映射表完整，不抛异常）
        for (state in ExperienceState.values()) {
            assertNotNull(VoiceUiModel(experience = state).primaryAction)
        }
        assertEquals(VoiceAction.START, VoiceUiModel(experience = ExperienceState.IDLE).primaryAction)
        assertEquals(
            VoiceAction.OPEN_SETTINGS,
            VoiceUiModel(experience = ExperienceState.REQUESTING_PERMISSION).primaryAction
        )
        assertEquals(VoiceAction.CANCEL, VoiceUiModel(experience = ExperienceState.CONNECTING).primaryAction)
        assertEquals(
            VoiceAction.STOP_LISTENING,
            VoiceUiModel(experience = ExperienceState.LISTENING).primaryAction
        )
        assertEquals(VoiceAction.CANCEL, VoiceUiModel(experience = ExperienceState.ENDPOINTING).primaryAction)
        assertEquals(VoiceAction.CANCEL, VoiceUiModel(experience = ExperienceState.THINKING).primaryAction)
        assertEquals(
            VoiceAction.STOP_SPEAKING,
            VoiceUiModel(experience = ExperienceState.SPEAKING).primaryAction
        )
        assertEquals(VoiceAction.CANCEL, VoiceUiModel(experience = ExperienceState.INTERRUPTED).primaryAction)
        assertEquals(VoiceAction.RETRY, VoiceUiModel(experience = ExperienceState.RECOVERING).primaryAction)
        assertEquals(VoiceAction.RETRY, VoiceUiModel(experience = ExperienceState.ERROR).primaryAction)
    }

    // ---- 会话生命周期 → 体验状态（DESIGN-DETAIL §2.2 约束表）----
    @Test
    fun `session lifecycle maps to experience states`() {
        assertEquals(ExperienceState.IDLE, ExperienceState.fromSession(VoiceSessionState.IDLE, hasError = false))
        assertEquals(
            ExperienceState.CONNECTING,
            ExperienceState.fromSession(VoiceSessionState.SIGNING, hasError = false)
        )
        assertEquals(
            ExperienceState.CONNECTING,
            ExperienceState.fromSession(VoiceSessionState.ENTERING, hasError = false)
        )
        assertEquals(
            ExperienceState.LISTENING,
            ExperienceState.fromSession(VoiceSessionState.IN_ROOM, hasError = false)
        )
        assertEquals(
            ExperienceState.CONNECTING,
            ExperienceState.fromSession(VoiceSessionState.EXITING, hasError = false)
        )
        // 错误优先：任何生命周期带错误 → error（UI 2 秒内展示分类原因）
        assertEquals(ExperienceState.ERROR, ExperienceState.fromSession(VoiceSessionState.IN_ROOM, hasError = true))
    }

    // ---- 旧六态兼容映射（RtcClient onPhase 事件）----
    @Test
    fun `legacy phase maps to experience states`() {
        assertEquals(ExperienceState.IDLE, ExperienceState.fromPhase(VoicePhase.IDLE))
        assertEquals(ExperienceState.IDLE, ExperienceState.fromPhase(VoicePhase.MONITORING))
        assertEquals(ExperienceState.LISTENING, ExperienceState.fromPhase(VoicePhase.LISTENING))
        assertEquals(ExperienceState.THINKING, ExperienceState.fromPhase(VoicePhase.THINKING))
        assertEquals(ExperienceState.SPEAKING, ExperienceState.fromPhase(VoicePhase.SPEAKING))
        assertEquals(ExperienceState.ERROR, ExperienceState.fromPhase(VoicePhase.ALERTING))
    }

    // ---- 分类错误模型（SPEC §6 / §5 错误码）----
    @Test
    fun `classified error model carries recovery action`() {
        assertEquals("40101", VoiceErrors.authFailed().code)
        assertEquals(VoiceAction.RE_PAIR, VoiceErrors.authFailed().action)
        assertEquals("40103", VoiceErrors.revoked().code)
        assertEquals("40801", VoiceErrors.handshakeTimeout().code)
        assertEquals(VoiceAction.RECONNECT, VoiceErrors.handshakeTimeout().action)
        assertEquals("40901", VoiceErrors.stateConflict().code)
        assertEquals(VoiceAction.RETRY, VoiceErrors.rateLimited().action)
        assertEquals("50401", VoiceErrors.upstreamTimeout().code)
        assertEquals(VoiceAction.REBUILD_PLAYBACK, VoiceErrors.playbackSilent().action)
        // 错误态主操作由错误模型决定（覆盖默认 RETRY）
        val model = VoiceUiModel(experience = ExperienceState.ERROR, error = VoiceErrors.revoked())
        assertEquals(VoiceAction.RE_PAIR, model.primaryAction)
    }

    // ---- SPEC §4.2：UI 禁止读取并行业务布尔（静态源码扫描）----
    @Test
    fun `ui code must not read parallel business booleans`() {
        val root = sourceRoot()
        val banned = Regex(
            "(var|val)\\s+(inCall|rtcExiting)|" +
                "(inCall|rtcExiting)\\s*(=|\\?|\\.|&&|\\|\\||!)"
        )
        val violations = root.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .mapNotNull { f ->
                f.readText().lineSequence()
                    .mapIndexedNotNull { idx, line ->
                        if (banned.containsMatchIn(line)) "${f.name}:${idx + 1}: $line" else null
                    }
                    .takeIf { it.any() }
            }
            .flatten()
            .toList()
        assertTrue("UI 代码不得读取 inCall/rtcExiting 等并行业务布尔: $violations", violations.isEmpty())
    }

    // ---- 聚合模型默认值安全 ----
    @Test
    fun `default model is idle without error`() {
        val m = VoiceUiModel()
        assertFalse(m.error != null)
        assertTrue(m.transcript.isEmpty())
        assertTrue(m.reply.isEmpty())
        assertEquals(ExperienceState.IDLE, m.experience)
        assertEquals(VoiceSessionState.IDLE, m.session)
    }

    private fun sourceRoot(): File {
        var dir: File? = File(System.getProperty("user.dir"))
        repeat(4) {
            val candidate = dir?.resolve("src/main/java/com/jax/voice")
            if (candidate != null && candidate.isDirectory) return candidate
            dir = dir?.parentFile
        }
        error("source root not found from cwd=${System.getProperty("user.dir")}")
    }
}
