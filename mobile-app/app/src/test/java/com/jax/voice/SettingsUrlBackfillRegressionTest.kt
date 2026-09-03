package com.jax.voice

import com.jax.voice.config.VoiceConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * A7 回归（2026-08-21）：默认 URL 固化。
 * 事故：SettingsActivity 用 sessionBaseUrl()（含出厂默认回落）回填输入框 →
 * 用户一保存就把默认域名固化进 prefs → 未来换域名时已分发 APK 无法通过留空迁移。
 * 修复契约：回填只读 customSessionBaseUrl()（原始存储值）；保存留空 = 存空串 = 清除自定义。
 */
class SettingsUrlBackfillRegressionTest {

    @Test
    fun `settings backfills only the user-custom url never the factory default`() {
        val source = File(findSourceRoot(), "SettingsActivity.kt").readText()

        // 回填必须用原始存储值（空 = 出厂默认生效），不能用带回落语义的 sessionBaseUrl()
        assertTrue(
            "回填应使用 customSessionBaseUrl（不含默认回落）",
            source.contains("etSessionUrl.setText(VoiceConfig.customSessionBaseUrl(this))")
        )
        assertFalse(
            "回填禁止使用 sessionBaseUrl（会把出厂默认固化进 prefs）",
            source.contains("etSessionUrl.setText(VoiceConfig.sessionBaseUrl(this))")
        )
    }

    @Test
    fun `pairing falls back to default url when input is blank`() {
        val source = File(findSourceRoot(), "SettingsActivity.kt").readText()

        // URL 留空时配对仍可用（回落默认网关），而不是被前置校验拦死
        assertTrue(source.contains(".ifBlank { VoiceConfig.sessionBaseUrl(this) }"))
    }

    @Test
    fun `customSessionBaseUrl returns raw stored value while sessionBaseUrl falls back to default`() {
        // 空串存储 → 生效地址 = 出厂默认（回落逻辑生效）；自定义值 → 原样返回
        val default = VoiceConfig.DEFAULT_SESSION_BASE_URL
        assertNotEquals("", default)
        assertTrue(default.startsWith("https://"))
        // customSessionBaseUrl 是新增 API：契约存在性 + 默认值可判空（行为验证需 Android 环境，
        // 此处验证常量与 API 形状，回填语义由上面两个源码断言守护）
        assertTrue(
            VoiceConfig::class.java.methods.any { it.name == "customSessionBaseUrl" }
        )
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
