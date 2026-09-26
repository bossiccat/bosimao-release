package com.jax.voice.util

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log

/**
 * 真机取证前可观测性缺口填补（场景4 下行播放/蓝牙/耳机/音频焦点 与 场景5 锁屏/后台/网络切换）。
 *
 * 商业发布门禁 android-duplex-audio 要求 6 个场景的现场取证，而场景4/场景5 在 App 层此前
 * 零可观测输出（全仓 grep `requestAudioFocus|ACTION_SCREEN_OFF|CONNECTIVITY_ACTION|registerReceiver|
 * ACTION_HEADSET_PLUG|AudioBecomingNoisy` 零命中）。本组件在取证开始前补上设备环境观测，
 * 使真机 logcat / diag_log.txt 对这两类事件可采。
 *
 * 铁律：纯观测，零行为变更。只打日志，不请求音频焦点、不改路由、不加任何影响行为的逻辑。
 * 不触碰 VoiceSessionCoordinator / MicRecorder / RealCustomAudioSource / net 包下现有代码。
 *
 * 每条事件同时写两处：
 *   - `Log.i("DeviceEnv", ...)`  → logcat 可采
 *   - `DiagLog.log("DeviceEnv", ...)` → 私有 diag 文件（与现有 Rtc/App 用法一致）
 */
class DeviceEnvObserver(private val ctx: Context) {

    companion object {
        const val TAG = "DeviceEnv"

        /**
         * 纯函数：AudioDeviceInfo.type → 用户可读标签（可 JVM 单测，不依赖 Android 运行时）。
         * 覆盖取证相关的几类下行播放设备；未知类型回退为 type_<n> 以便真机现场直接识别数值。
         */
        fun deviceTypeLabel(type: Int): String = when (type) {
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth_a2dp"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth_sco"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_headset"
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "wired_headphones"
            AudioDeviceInfo.TYPE_USB_HEADSET -> "usb_headset"
            AudioDeviceInfo.TYPE_USB_DEVICE -> "usb_device"
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "builtin_speaker"
            AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "builtin_earpiece"
            AudioDeviceInfo.TYPE_HEARING_AID -> "hearing_aid"
            else -> "type_$type"
        }

        /**
         * 纯函数：由 wifi / cellular 两个布尔标志推导网络传输标注（可 JVM 单测）。
         * 真实回调里从 [NetworkCapabilities.hasTransport] 取这两个布尔再传入，避免测试依赖
         * Android 运行时对象。
         */
        fun transportLabelFromFlags(hasWifi: Boolean, hasCellular: Boolean): String = when {
            hasWifi && hasCellular -> "wifi+cellular"
            hasWifi -> "wifi"
            hasCellular -> "cellular"
            else -> "other"
        }

        /** 纯函数：构建一条事件文案（去重以文本为准）。 */
        fun buildEventText(prefix: String, detail: String): String = "$prefix: $detail"
    }

    /** 去重器：按类别记忆上一条文本，连续重复事件跳过，避免 logcat / diag 风暴。可 JVM 单测。 */
    class Dedup {
        private val lastByCategory = mutableMapOf<String, String>()

        /** 返回 true 表示这是新事件应记录；false 表示与同类别上一条完全一致应跳过。 */
        fun shouldEmit(category: String, text: String): Boolean {
            val prev = lastByCategory[category]
            if (prev == text) return false
            lastByCategory[category] = text
            return true
        }

        fun reset() = lastByCategory.clear()
    }

    private val dedup = Dedup()

    private val audioManager: AudioManager by lazy {
        ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    }
    private val connectivityManager: ConnectivityManager by lazy {
        ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    }

    @Volatile private var started = false

    private val audioDeviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<AudioDeviceInfo>?) {
            added?.forEach { d ->
                emit("audio_device", "added type=${deviceTypeLabel(d.type)} id=${d.id} product=${d.productName ?: ""}")
            }
        }

        override fun onAudioDevicesRemoved(removed: Array<AudioDeviceInfo>?) {
            removed?.forEach { d ->
                emit("audio_device", "removed type=${deviceTypeLabel(d.type)} id=${d.id}")
            }
        }
    }

    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context?, intent: Intent?) {
            when (intent?.action) {
                Intent.ACTION_SCREEN_OFF -> emit("screen", "SCREEN_OFF")
                Intent.ACTION_SCREEN_ON -> emit("screen", "SCREEN_ON")
                Intent.ACTION_USER_PRESENT -> emit("screen", "USER_PRESENT")
            }
        }
    }

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            emit("network", "available net=$network")
        }

        override fun onLost(network: Network) {
            emit("network", "lost net=$network")
        }

        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
            val label = transportLabelFromFlags(
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI),
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)
            )
            emit("network", "capabilities net=$network transport=$label")
        }
    }

    /** 同时落 logcat 与 diag 文件；按类别去重。 */
    private fun emit(category: String, text: String) {
        if (!dedup.shouldEmit(category, text)) return
        Log.i(TAG, text)
        DiagLog.log(TAG, text)
    }

    fun start() {
        if (started) return
        started = true
        // 基线快照：记录当前已连接设备，便于真机取证起点对齐
        try {
            audioManager.getDevices(AudioManager.GET_DEVICES_ALL)?.forEach { d ->
                emit("audio_device", "baseline type=${deviceTypeLabel(d.type)} id=${d.id} product=${d.productName ?: ""}")
            }
        } catch (t: Throwable) {
            Log.w(TAG, "baseline device snapshot failed: ${t.message}")
        }
        try {
            audioManager.registerAudioDeviceCallback(audioDeviceCallback, null)
        } catch (t: Throwable) {
            Log.w(TAG, "registerAudioDeviceCallback failed: ${t.message}")
        }
        try {
            val filter = IntentFilter().apply {
                addAction(Intent.ACTION_SCREEN_OFF)
                addAction(Intent.ACTION_SCREEN_ON)
                addAction(Intent.ACTION_USER_PRESENT)
            }
            // 不导出、不接收粘滞：仅本进程内动态注册，符合零行为变更
            ctx.registerReceiver(screenReceiver, filter)
        } catch (t: Throwable) {
            Log.w(TAG, "registerReceiver(screen) failed: ${t.message}")
        }
        try {
            val req = NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build()
            connectivityManager.registerNetworkCallback(req, networkCallback)
        } catch (t: Throwable) {
            Log.w(TAG, "registerNetworkCallback failed: ${t.message}")
        }
        emit("lifecycle", "observer started")
    }

    fun stop() {
        if (!started) return
        started = false
        try {
            audioManager.unregisterAudioDeviceCallback(audioDeviceCallback)
        } catch (t: Throwable) {
            Log.w(TAG, "unregisterAudioDeviceCallback failed: ${t.message}")
        }
        try {
            ctx.unregisterReceiver(screenReceiver)
        } catch (t: Throwable) {
            Log.w(TAG, "unregisterReceiver(screen) failed: ${t.message}")
        }
        try {
            connectivityManager.unregisterNetworkCallback(networkCallback)
        } catch (t: Throwable) {
            Log.w(TAG, "unregisterNetworkCallback failed: ${t.message}")
        }
        // 反注册后再记一条生命周期端点，便于与 start 配对
        dedup.reset()
        Log.i(TAG, "observer stopped")
        DiagLog.log(TAG, "observer stopped")
    }
}
