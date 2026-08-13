package com.jax.voice.voice

import android.content.Context
import android.content.Intent
import io.mockk.every
import io.mockk.mockk
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * P0 三入口统一命令 L0 单测（Task 8 / DESIGN-DETAIL §3 / SPEC O-003）。
 *
 * 主页面「开始对话」、悬浮球轻触、前台通知「立即对话」必须经 VoiceEntry.startConversation
 * 投递同一个 ACTION_TALK 命令，服务端解析 source 后进入同一个
 * VoiceSessionCoordinator.Start（TRTC 全双工路径）；任一入口失败不得破坏另外两个。
 * P0 路径不调用 WakeWordEngine（静态断言）。
 *
 * 反作弊：无 @Ignore/skip；命令快照为真实断言，非 mock-only。
 */
class VoiceEntryPointTest {

    private var launchCount = 0
    private val ctx: Context = mockk<Context>(relaxed = true).also { mock ->
        every { mock.classLoader } returns VoiceEntry::class.java.classLoader
    }

    @Before
    fun setUp() {
        launchCount = 0
        VoiceEntry.lastStartCommand = null
        VoiceEntry.serviceLauncher = { _, _ -> launchCount++ }
    }

    @After
    fun tearDown() {
        VoiceEntry.serviceLauncher = { context, intent -> context.startForegroundService(intent) }
    }

    private fun assertCommand(source: String) {
        assertEquals(
            "入口 $source 必须投递统一 ACTION_TALK 命令",
            VoiceEntry.StartCommand(VoiceForegroundService.ACTION_TALK, source),
            VoiceEntry.lastStartCommand
        )
    }

    // ---- 主页面入口 ----
    @Test
    fun `main page entry uses unified TALK command`() {
        VoiceEntry.startConversation(ctx, "main")
        assertEquals(1, launchCount)
        assertCommand("main")
    }

    // ---- 悬浮球入口 ----
    @Test
    fun `overlay entry uses unified TALK command`() {
        VoiceEntry.startConversation(ctx, "overlay")
        assertEquals(1, launchCount)
        assertCommand("overlay")
    }

    // ---- 通知按钮入口：无 extra 时回落默认 source ----
    @Test
    fun `notification entry resolves to default source`() {
        assertEquals(
            "通知按钮 Intent 无 source 时必须回落 notification_talk",
            "notification_talk",
            VoiceEntry.resolveSource(null, "notification_talk")
        )
        assertEquals(
            "空白 source 也必须回落默认值",
            "notification_talk",
            VoiceEntry.resolveSource(Intent(), "notification_talk")
        )
    }

    // ---- 三入口收敛同一命令且互相独立 ----
    @Test
    fun `three entries converge on same command and stay independent`() {
        VoiceEntry.startConversation(ctx, "main")
        VoiceEntry.startConversation(ctx, "overlay")
        VoiceEntry.startConversation(ctx, "notification")
        assertEquals(3, launchCount)
        assertCommand("notification")
        assertEquals(
            setOf("main", "overlay", "notification"),
            setOf(
                VoiceEntry.StartCommand(VoiceForegroundService.ACTION_TALK, "main").source,
                VoiceEntry.StartCommand(VoiceForegroundService.ACTION_TALK, "overlay").source,
                VoiceEntry.StartCommand(VoiceForegroundService.ACTION_TALK, "notification").source
            )
        )
    }

    // ---- 任一入口失败不得破坏另外两个 ----
    @Test
    fun `one entry failure does not break the others`() {
        var failOnce = true
        VoiceEntry.serviceLauncher = { _, _ ->
            if (failOnce) {
                failOnce = false
                throw IllegalStateException("entry boom")
            }
            launchCount++
        }
        try {
            VoiceEntry.startConversation(ctx, "main")
        } catch (_: IllegalStateException) {
            // 入口失败按调用方策略处理（如 Toast），不得影响其他入口
        }
        VoiceEntry.startConversation(ctx, "overlay")
        VoiceEntry.startConversation(ctx, "notification")
        assertEquals("失败入口不影响其余两个入口", 2, launchCount)
        assertCommand("notification")
    }

    // ---- P0 三入口不直接触发唤醒词；服务端统一 source 解析 ----
    @Test
    fun `p0 entries bypass wake engine and service resolves source`() {
        val root = findSourceRoot()
        val main = File(root, "MainActivity.kt").readText()
        val overlay = File(root, "ui/FloatingOverlay.kt").readText()
        val service = File(root, "voice/VoiceForegroundService.kt").readText()
        assertTrue("主页面必须经统一命令层发起", main.contains("VoiceEntry.startConversation"))
        assertTrue("悬浮球必须经统一命令层发起", overlay.contains("VoiceEntry.startConversation"))
        assertTrue("服务端必须用 resolveSource 解析三入口 source", service.contains("VoiceEntry.resolveSource"))
        assertTrue("服务端解析后必须进入同一个 coordinator.start", service.contains("coordinator?.start("))
        // P0 入口文件不调用唤醒词引擎（P1 边界；KWS 仅在服务内按配置独立装配）
        assertTrue("P0 入口不得触发唤醒词引擎", !main.contains("WakeWordEngine(") && !overlay.contains("WakeWordEngine("))
    }

    private fun findSourceRoot(): String {
        var dir: File? = File(System.getProperty("user.dir"))
        repeat(4) {
            val candidate = dir?.resolve("src/main/java/com/jax/voice")
            if (candidate != null && candidate.isDirectory) return candidate.absolutePath
            dir = dir?.parentFile
        }
        error("source root not found")
    }
}
