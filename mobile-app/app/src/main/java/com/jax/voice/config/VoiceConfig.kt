package com.jax.voice.config

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * 本地配置（SharedPreferences）。
 *
 * v0.6.0（TRTC 重构，ADR-012 / MOBILE-INTEGRATION）：
 * - 语音链路统一走 TRTC，自研 WS 中继（LAN 直连 / 云端 relay / 配对码 / E2EE）已废弃删除。
 * - E2EE 由 RTC 加密替代；手机端不持有 SecretKey（唯一存云函数环境变量）。
 * - 会话签发接口地址（session_base_url）由设置页填写，客户端拼 /api/v1/voice/session 拉
 *   room_id + userSig 进房。
 * - 保留：设备 ID（进房关联）、唤醒词 / KWS 灵敏度 / 悬浮窗（本地交互配置）。
 */
object VoiceConfig {
    private const val PREFS = "jax_voice_config"

    // ---------- 配置迁移（v0.4.6 新增；v0.6.0 TRTC 重构后连接配置废弃） ----------
    // 说明：旧版 prefs 残留（conn_mode/relay_url/server_url/pairing_code/e2ee_*）在新版已不再读取，
    // 无需写入；迁移仅负责初始化保留项（唤醒开关 + 会话签发地址）。
    private const val KEY_CONFIG_VERSION = "config_version"
    private const val CONFIG_VERSION = 3

    /** 版本升级时自动迁移：初始化保留项为开发态出厂默认（零配置即可用） */
    fun migrateIfNeeded(context: Context) {
        val p = prefs(context)
        if (p.getInt(KEY_CONFIG_VERSION, 0) >= CONFIG_VERSION) return
        p.edit()
            .putBoolean("wake_enabled", WAKE_DEFAULT_ENABLED)
            // v0.6.0：TRTC 重构，会话签发接口地址默认留空（设置页填写提示）
            .putString("session_base_url", "")
            .putInt(KEY_CONFIG_VERSION, CONFIG_VERSION)
            .apply()
    }

    const val DEFAULT_THRESHOLD = 0.25f
    const val THRESHOLD_MIN = 0.10f
    const val THRESHOLD_MAX = 0.50f
    const val WAKEWORD = "波斯猫"

    /**
     * 唤醒词拼音音素串（sherpa-onnx KWS 建模单元 = 拼音 声母+韵母，见模型 keywords.txt）：
     * 波斯猫 = b ō s ī m āo（tokens.txt 中 b=29/ō=140/s=27/ī=12/m=58/āo=73 均存在）。
     * 模型内置关键词均为该格式（如 `n ǐ h ǎo j ūn g ē @你好军哥`），`@` 后为显示名。
     * createStream 必须传拼音音素串而非中文（中文 token 化失败 → KWS 永不触发/边界崩溃）。
     */
    const val WAKEWORD_PINYIN = "b ō s ī m āo @波斯猫"

    // VAD：静音超时 1.5s 自动 speech_end（spec §4.6）；15s 仅作未检出语音时的硬兜底（spec §7.4 V-5）
    const val VAD_SILENCE_MS = 1_500L
    const val VAD_IDLE_FALLBACK_MS = 15_000L

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // ---------- 设备 ID（生成后持久化；进房用户标识/会话关联，spec §7.1） ----------
    fun deviceId(context: Context): String {
        val prefs = prefs(context)
        prefs.getString("device_id", null)?.let { if (it.isNotBlank()) return it }
        val id = newDeviceId()
        prefs.edit().putString("device_id", id).apply()
        return id
    }

    fun regenerateDeviceId(context: Context): String {
        val id = newDeviceId()
        prefs(context).edit().putString("device_id", id).apply()
        return id
    }

    private fun newDeviceId(): String = "jax-" + UUID.randomUUID().toString().replace("-", "").take(8)

    // ---------- TRTC 会话（v0.6.0 重构，ADR-012 / MOBILE-INTEGRATION §2.2） ----------
    // 自研 WS 中继（LAN/RELAY）已废弃删除；语音链路统一走 TRTC。手机端不持有 SecretKey
    // （唯一存云函数环境变量），进房凭证由云函数签发接口下发（userSig 短时效）。
    // 注意：SDKAppID 非密钥（控制台公开标识），可硬编码；SecretKey 严禁进 APK。
    const val SDK_APP_ID = 1600155678

    /** TRTC 房间号前缀（对齐 PC 端 TRTC_ROOM_PREFIX；云函数签发的 room_id 已含前缀，此处仅文档说明） */
    const val TRTC_ROOM_PREFIX = "jax-"

    /**
     * 会话签发接口 base URL（设置页填写，默认留空提示填写）。
     * 形如 https://<云函数域名> 或 https://<host>/<prefix>；客户端拼 /api/v1/voice/session。
     */
    fun sessionBaseUrl(context: Context): String =
        prefs(context).getString("session_base_url", "") ?: ""

    fun setSessionBaseUrl(context: Context, url: String) {
        prefs(context).edit().putString("session_base_url", url.trim()).apply()
    }

    // ---------- 唤醒词 / KWS 灵敏度 / 悬浮窗 ----------
    // ⚠️ v0.4.4：唤醒词默认关闭（sherpa JNI 是原生崩溃最大嫌疑；默认禁用 = 完全不加载模型，
    //    用悬浮球轻触/通知按钮触发对话。设置页可开——开启后若闪退 = 100% 实锤 sherpa 崩溃源）
    const val WAKE_DEFAULT_ENABLED = false

    fun wakeEnabled(context: Context): Boolean =
        prefs(context).getBoolean("wake_enabled", WAKE_DEFAULT_ENABLED)

    fun setWakeEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean("wake_enabled", enabled).apply()
    }

    /** KWS 触发阈值（越低越灵敏），spec §5.2 灵敏度可调；同时复用作 VAD RMS 门限 */
    fun threshold(context: Context): Float =
        prefs(context).getFloat("kws_threshold", DEFAULT_THRESHOLD)

    fun setThreshold(context: Context, value: Float) {
        prefs(context).edit().putFloat("kws_threshold", value.coerceIn(THRESHOLD_MIN, THRESHOLD_MAX)).apply()
    }

    fun overlayEnabled(context: Context): Boolean =
        prefs(context).getBoolean("overlay_enabled", true)

    fun setOverlayEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean("overlay_enabled", enabled).apply()
    }
}
