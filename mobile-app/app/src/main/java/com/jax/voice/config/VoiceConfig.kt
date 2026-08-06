package com.jax.voice.config

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * 本地配置（SharedPreferences）。
 *
 * M1：局域网直连 PC voice 网关（spec §6.2/§8.1）：
 *   ws://<PC-LAN-IP>:8000/api/v1/voice/stream
 * M2 新增：
 * - 连接模式（lan / relay，spec §6.2）：
 *   局域网直连  ws://<PC IP>:8000/api/v1/voice/stream
 *   云端中继    wss://<relay 地址>/relay/ws + pair 配对帧（与 PC 配对后透传）
 * - E2EE（AES-GCM，32 字节密钥，spec §6.4）：密钥可粘贴，默认开发密钥；
 *   清空密钥 = 明文模式（UI 提示"未加密"）
 * - 设备 ID（生成后持久化，spec §7.1 device_id）：PC 配对 / 中继会话关联用
 */
object VoiceConfig {
    private const val PREFS = "jax_voice_config"

    // ---------- 配置迁移（v0.4.6 新增；v0.6.0 TRTC 重构后连接配置废弃） ----------
    // 问题：旧版 prefs 残留（如 conn_mode=lan 局域网、旧配对码、旧地址）会让用户装新版后
    // 依然连不上中继（出差场景必挂）。解决：版本升级时强制重置关键连接配置为出厂默认。
    private const val KEY_CONFIG_VERSION = "config_version"
    private const val CONFIG_VERSION = 3

    /** 版本升级时自动迁移：重置连接相关配置为开发态出厂默认（零配置即可用） */
    fun migrateIfNeeded(context: Context) {
        val p = prefs(context)
        if (p.getInt(KEY_CONFIG_VERSION, 0) >= CONFIG_VERSION) return
        p.edit()
            .putString("conn_mode", MODE_RELAY)
            .putString("relay_url", DEFAULT_RELAY)
            .putString("server_url", DEFAULT_SERVER)
            .putString("pairing_code", DEFAULT_PAIRING_CODE)
            .putString("e2ee_key", DEFAULT_E2EE_KEY)
            .putBoolean("e2ee_enabled", true)
            .putBoolean("wake_enabled", WAKE_DEFAULT_ENABLED)
            // v0.6.0：TRTC 重构，会话签发接口地址默认留空（设置页填写提示）
            .putString("session_base_url", "")
            .putInt(KEY_CONFIG_VERSION, CONFIG_VERSION)
            .apply()
    }

    // 连接模式
    const val MODE_LAN = "lan"
    const val MODE_RELAY = "relay"

    const val DEFAULT_SERVER = "ws://192.168.1.100:8000/api/v1/voice/stream"
    // 中继端点：/relay/ws（与 be-relay 中继服务对应；生产替换为实际域名）
    const val DEFAULT_RELAY = "wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws"
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

    // E2EE 默认开发密钥：32 字符 ASCII，经 SHA-256 派生 32 字节 AES 密钥；
    // PC relay 的 RELAY_E2EE_KEY 开发态必须与此字符串一致才能互通（见 README §E2EE）
    const val DEFAULT_E2EE_KEY = "jax-voice-dev-e2ee-20260803-0001"

    // VAD（M2）：静音超时 1.5s 自动 speech_end（spec §4.6）；15s 仅作未检出语音时的硬兜底（spec §7.4 V-5）
    const val VAD_SILENCE_MS = 1_500L
    const val VAD_IDLE_FALLBACK_MS = 15_000L

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // ---------- 连接模式 ----------
    fun connectionMode(context: Context): String =
        prefs(context).getString("conn_mode", MODE_LAN) ?: MODE_LAN

    fun setConnectionMode(context: Context, mode: String) {
        prefs(context).edit().putString("conn_mode", if (mode == MODE_RELAY) MODE_RELAY else MODE_LAN).apply()
    }

    /** 当前生效的连接地址（按连接模式取局域网直连或中继） */
    fun activeServerUrl(context: Context): String =
        if (connectionMode(context) == MODE_RELAY) relayUrl(context) else serverUrl(context)

    // ---------- 局域网直连地址 ----------
    fun serverUrl(context: Context): String =
        prefs(context).getString("server_url", DEFAULT_SERVER) ?: DEFAULT_SERVER

    fun setServerUrl(context: Context, url: String) {
        prefs(context).edit().putString("server_url", url.trim()).apply()
    }

    // ---------- 中继地址 ----------
    fun relayUrl(context: Context): String =
        prefs(context).getString("relay_url", DEFAULT_RELAY) ?: DEFAULT_RELAY

    fun setRelayUrl(context: Context, url: String) {
        prefs(context).edit().putString("relay_url", url.trim()).apply()
    }

    // ---------- 配对码（spec §6.4：PC 生成 6 位配对码；手机输入后随 pair 帧发送） ----------
    // 默认预填 JAX2026（与 PC 中继 relay_client --pairing-code JAX2026 一致；用户选中继后无需手动输入）
        // ⚠️ 开发阶段预置中继 token（与云端 RELAY_TOKEN 一致，否则被 1008 拒绝）；
    //    正式上线前必须从源码移除并在云端轮换（token 进 APK 即公开）
    const val RELAY_TOKEN = "yOeLI165VkN3DTJ1xbHeTG8iohrgtbrVG3D/gsjPQPg="
    const val DEFAULT_PAIRING_CODE = "JAX2026"

    private val PAIRING_CODE_PATTERN = Regex("^[A-Za-z0-9]{4,12}$")

    fun pairingCode(context: Context): String {
        val stored = prefs(context).getString("pairing_code", "") ?: ""
        // 合法值 = 默认值 或 4-12 位字母数字（PC /relay/pair 生成 6 位数字，默认 JAX2026 为 7 位字母数字）
        if (stored == DEFAULT_PAIRING_CODE || PAIRING_CODE_PATTERN.matches(stored)) return stored
        // 残留无效值（如手输漏位/旧版 maxLength=6 截断的 JAX202）→ 重置为默认 JAX2026
        prefs(context).edit().putString("pairing_code", DEFAULT_PAIRING_CODE).apply()
        return DEFAULT_PAIRING_CODE
    }

    fun setPairingCode(context: Context, code: String) {
        prefs(context).edit().putString("pairing_code", code.trim()).apply()
    }

    // ---------- 设备 ID（生成后持久化；PC 配对/中继会话关联，spec §7.1） ----------
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

    // ---------- E2EE（AES-GCM） ----------
    fun e2eeEnabled(context: Context): Boolean =
        prefs(context).getBoolean("e2ee_enabled", true)

    fun setE2eeEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean("e2ee_enabled", enabled).apply()
    }

    /** E2EE 密钥字符串；空串 = 明文模式（未加密）。默认开发密钥（与 PC RELAY_E2EE_KEY 一致） */
    fun e2eeKey(context: Context): String =
        prefs(context).getString("e2ee_key", DEFAULT_E2EE_KEY) ?: ""

    fun setE2eeKey(context: Context, key: String) {
        prefs(context).edit().putString("e2ee_key", key.trim()).apply()
    }

    /** 是否实际加密：开关开 且 密钥非空 */
    fun isE2eeActive(context: Context): Boolean =
        e2eeEnabled(context) && e2eeKey(context).isNotBlank()

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

    // ---------- 唤醒词 / KWS 灵敏度 / 悬浮窗（M1） ----------
    // ⚠️ v0.4.4：唤醒词默认关闭（sherpa JNI 是原生崩溃最大嫌疑；默认禁用 = 完全不加载模型，
    //    用悬浮球轻触/通知按钮触发对话。设置页可开——开启后若闪退 = 100% 实锤 sherpa 崩溃源）
    const val WAKE_DEFAULT_ENABLED = false

    fun wakeEnabled(context: Context): Boolean =
        prefs(context).getBoolean("wake_enabled", WAKE_DEFAULT_ENABLED)

    fun setWakeEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean("wake_enabled", enabled).apply()
    }

    /** KWS 触发阈值（越低越灵敏），spec §5.2 灵敏度可调；M2 同时复用作 VAD RMS 门限 */
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
