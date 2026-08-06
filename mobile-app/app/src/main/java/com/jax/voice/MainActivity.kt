package com.jax.voice

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.jax.voice.config.VoiceConfig
import com.jax.voice.ui.FloatingOverlay
import com.jax.voice.ui.WaveformView
import com.jax.voice.voice.ConnectionState
import com.jax.voice.voice.ServiceState
import com.jax.voice.voice.VoiceController
import com.jax.voice.voice.VoiceForegroundService
import com.jax.voice.voice.VoicePhase
import kotlinx.coroutines.launch

/**
 * M1 状态页：连接状态 / 唤醒状态 / 波形占位 + 启动停止 + 权限/白名单引导（spec §4.5/§4.1）。
 *
 * 崩溃可达性：onCreate 整体 try-catch（任何初始化异常 → Toast 而非闪退）；
 * 启动时检查 JaxApp 崩溃日志 → AlertDialog 展示崩溃摘要（知道了/分享），展示后清空。
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
    }

    private val overlay by lazy { FloatingOverlay(this) }

    private lateinit var tvServiceState: TextView
    private lateinit var dotPhase: android.view.View
    private lateinit var tvPhase: TextView
    private lateinit var tvConnection: TextView
    private lateinit var tvWakeCount: TextView
    private lateinit var tvServer: TextView
    private lateinit var tvLastError: TextView
    private lateinit var waveform: WaveformView
    private lateinit var btnToggleListen: Button
    private lateinit var btnTalk: Button

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { /* 结果在 onResume 二次检查处理 */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            onCreateSafe(savedInstanceState)
        } catch (t: Throwable) {
            // 全路径防御：任何初始化异常不闪退 —— Toast 展示错误信息（用户可见）
            Log.e(TAG, "onCreate crashed: ${t.message}", t)
            try {
                Toast.makeText(
                    this,
                    getString(R.string.init_error_toast, t.message ?: t.javaClass.simpleName),
                    Toast.LENGTH_LONG
                ).show()
            } catch (_: Throwable) {
                // Toast 自身失败也绝不能崩
            }
        }
    }

    private fun onCreateSafe(savedInstanceState: Bundle?) {
        setContentView(R.layout.activity_main)

        // v0.4.6：版本升级自动重置连接配置（防旧版 prefs 残留导致连不上中继）
        VoiceConfig.migrateIfNeeded(this)

        tvServiceState = findViewById(R.id.tvServiceState)
        dotPhase = findViewById(R.id.dotPhase)
        tvPhase = findViewById(R.id.tvPhase)
        tvConnection = findViewById(R.id.tvConnection)
        tvLastError = findViewById(R.id.tvLastError)
        tvWakeCount = findViewById(R.id.tvWakeCount)
        tvServer = findViewById(R.id.tvServer)
        waveform = findViewById(R.id.waveform)
        btnToggleListen = findViewById(R.id.btnToggleListen)
        btnTalk = findViewById(R.id.btnTalk)

        findViewById<Button>(R.id.btnSettings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        findViewById<Button>(R.id.btnOverlay).setOnClickListener { guideOverlay() }
        findViewById<Button>(R.id.btnBattery).setOnClickListener { guideBattery() }

        btnToggleListen.setOnClickListener {
            if (VoiceController.ui.value.service == ServiceState.RUNNING) {
                stopService(Intent(this, VoiceForegroundService::class.java))
            } else {
                startListening()
            }
        }
        btnTalk.setOnClickListener {
            try {
                startForegroundService(
                    Intent(this, VoiceForegroundService::class.java)
                        .setAction(VoiceForegroundService.ACTION_TALK)
                )
            } catch (e: Exception) {
                // Android 14 后台启动 mic 前台服务受限：引导用户先打开 App（spec §11-1）
                Toast.makeText(this, "请先打开 App 再使用", Toast.LENGTH_SHORT).show()
            }
        }

        observeUi()
        requestPermissionsIfNeeded()
        showCrashDialogIfAny()
    }

    override fun onResume() {
        super.onResume()
        refreshOverlay()
        requestPermissionsIfNeeded()
    }

    private fun observeUi() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                VoiceController.ui.collect { state ->
                    try {
                        tvPhase.text = phaseLabel(state.phase)
                        // 防御：background 可能为 null（布局变更）——判空后再 mutate，绝不 NPE
                        dotPhase.background?.let { b ->
                            dotPhase.background = b.mutate().apply {
                                if (this is android.graphics.drawable.GradientDrawable) {
                                    setColor(ContextCompat.getColor(this@MainActivity, phaseColor(state.phase)))
                                }
                            }
                        }
                        tvConnection.text = connectionLabel(state.connection)
                        tvConnection.setTextColor(
                            ContextCompat.getColor(this@MainActivity, connectionColor(state.connection))
                        )
                        tvWakeCount.text = state.wakeCount.toString()
                        // v0.6.0 TRTC：显示会话服务器（签发接口）地址
                        tvServer.text = VoiceConfig.sessionBaseUrl(this@MainActivity).ifBlank {
                            "未设置（设置页填写）"
                        }
                        // v0.4.7 诊断：连接失败原因显示/隐藏
                        if (state.lastError.isBlank()) {
                            tvLastError.visibility = android.view.View.GONE
                        } else {
                            tvLastError.visibility = android.view.View.VISIBLE
                            tvLastError.text = state.lastError
                        }

                        val running = state.service == ServiceState.RUNNING
                        tvServiceState.text = getString(
                            if (running) R.string.service_running else R.string.service_stopped
                        )
                        tvServiceState.setTextColor(
                            ContextCompat.getColor(
                                this@MainActivity,
                                if (running) R.color.conn_connected else R.color.jax_muted
                            )
                        )
                        btnToggleListen.text = getString(
                            if (running) R.string.btn_stop_listen else R.string.btn_start_listen
                        )
                        btnTalk.isEnabled = running

                        waveform.pushRms(if (state.phase == VoicePhase.MONITORING) state.rms * 0.5f else state.rms)

                        overlay.updatePhase(state.phase)
                        if (running) {
                            overlay.show()
                        } else {
                            overlay.hide()
                        }
                    } catch (t: Throwable) {
                        // collect 内任何异常不崩 UI 线程：记日志继续
                        Log.e(TAG, "observeUi collect crashed: ${t.message}", t)
                    }
                }
            }
        }
    }

    /** 崩溃日志弹窗：App 私有目录有日志 → 展示摘要（"知道了/分享"），展示后清空（避免每次启动重复弹窗） */
    private fun showCrashDialogIfAny() {
        try {
            val app = application as? JaxApp ?: return
            val summary = app.readCrashSummary() ?: return
            val dialog = AlertDialog.Builder(this)
                .setTitle(R.string.crash_dialog_title)
                .setMessage(getString(R.string.crash_dialog_message, summary))
                .setPositiveButton(R.string.crash_dialog_ok) { d, _ -> d.dismiss() }
                .setNegativeButton(R.string.crash_dialog_share) { _, _ -> shareCrashLog(summary) }
                .setNeutralButton(R.string.crash_dialog_copy) { _, _ -> copyCrashLog(summary) }
                .setOnDismissListener { app.clearCrashLog() }
                .create()
            dialog.show()
        } catch (t: Throwable) {
            // 弹窗失败绝不能崩
            Log.e(TAG, "crash dialog failed: ${t.message}", t)
        }
    }

    /** 复制崩溃日志到剪贴板（用户可粘贴到微信/记事本发给团队） */
    private fun copyCrashLog(summary: String) {
        try {
            val cm = getSystemService(android.content.ClipboardManager::class.java)
            cm.setPrimaryClip(android.content.ClipData.newPlainText("crash_log", summary))
            Toast.makeText(this, R.string.crash_copy_done, Toast.LENGTH_SHORT).show()
        } catch (t: Throwable) {
            Log.e(TAG, "copy crash log failed: ${t.message}", t)
        }
    }

    /** 分享崩溃日志：Intent.ACTION_SEND 发文本（用户可发微信给自己/PC） */
    private fun shareCrashLog(summary: String) {
        try {
            val send = Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_SUBJECT, getString(R.string.crash_share_subject))
                putExtra(Intent.EXTRA_TEXT, getString(R.string.crash_share_text, summary))
            }
            startActivity(Intent.createChooser(send, getString(R.string.crash_share_chooser)))
        } catch (t: Throwable) {
            Log.e(TAG, "share crash log failed: ${t.message}", t)
            try {
                Toast.makeText(this, R.string.crash_share_failed, Toast.LENGTH_SHORT).show()
            } catch (_: Throwable) {
            }
        }
    }

    private fun startListening() {
        val micOk = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (!micOk) {
            Toast.makeText(this, R.string.perm_mic_rationale, Toast.LENGTH_LONG).show()
            requestPermissionsIfNeeded()
            return
        }
        try {
            startForegroundService(
                Intent(this, VoiceForegroundService::class.java)
                    .setAction(VoiceForegroundService.ACTION_START)
            )
        } catch (e: Exception) {
            // Android 14 后台启动 mic 前台服务受限 / 系统策略拒绝 → Toast 提示而非崩溃
            Log.e(TAG, "start FGS failed: ${e.message}")
            try {
                Toast.makeText(this, "启动监听失败：${e.message}", Toast.LENGTH_LONG).show()
            } catch (_: Throwable) {
            }
        }
    }

    private fun requestPermissionsIfNeeded() {
        val needMic = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        val needNotif = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        if (needMic || needNotif) {
            val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
            if (needNotif) perms.add(Manifest.permission.POST_NOTIFICATIONS)
            permissionLauncher.launch(perms.toTypedArray())
        }
    }

    private fun guideOverlay() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
            startActivity(
                Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:$packageName")
                )
            )
        } else {
            Toast.makeText(this, "悬浮窗权限已开启", Toast.LENGTH_SHORT).show()
        }
    }

    private fun guideBattery() {
        val pm = getSystemService(PowerManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !pm.isIgnoringBatteryOptimizations(packageName)) {
            startActivity(
                Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName")
                )
            )
        } else {
            Toast.makeText(this, "已在电池白名单", Toast.LENGTH_SHORT).show()
        }
    }

    private fun refreshOverlay() {
        if (VoiceConfig.overlayEnabled(this) && VoiceController.ui.value.service == ServiceState.RUNNING) {
            overlay.show()
        }
    }

    private fun phaseLabel(phase: VoicePhase): String = when (phase) {
        VoicePhase.IDLE -> getString(R.string.phase_idle)
        VoicePhase.MONITORING -> getString(R.string.phase_monitoring)
        VoicePhase.LISTENING -> getString(R.string.phase_listening)
        VoicePhase.THINKING -> getString(R.string.phase_thinking)
        VoicePhase.SPEAKING -> getString(R.string.phase_speaking)
        VoicePhase.ALERTING -> getString(R.string.phase_alerting)
    }

    private fun phaseColor(phase: VoicePhase): Int = when (phase) {
        VoicePhase.IDLE -> R.color.state_idle
        VoicePhase.MONITORING -> R.color.state_monitoring
        VoicePhase.LISTENING -> R.color.state_listening
        VoicePhase.THINKING -> R.color.state_thinking
        VoicePhase.SPEAKING -> R.color.state_speaking
        VoicePhase.ALERTING -> R.color.state_alerting
    }

    private fun connectionLabel(state: ConnectionState): String = when (state) {
        ConnectionState.DISCONNECTED -> getString(R.string.conn_disconnected)
        ConnectionState.CONNECTING -> getString(R.string.conn_connecting)
        ConnectionState.CONNECTED -> getString(R.string.conn_connected)
    }

    private fun connectionColor(state: ConnectionState): Int = when (state) {
        ConnectionState.DISCONNECTED -> R.color.conn_disconnected
        ConnectionState.CONNECTING -> R.color.conn_connecting
        ConnectionState.CONNECTED -> R.color.conn_connected
    }
}
