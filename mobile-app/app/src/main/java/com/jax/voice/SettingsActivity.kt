package com.jax.voice

import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.EditText
import android.widget.SeekBar
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.jax.voice.config.VoiceConfig

/**
 * 设置页（v0.6.0 TRTC）：会话服务器（TRTC userSig 签发 base URL）/ 设备 ID / 唤醒词 /
 * 灵敏度 / 悬浮窗。修改即时持久化（VoiceConfig → SharedPreferences），下次启动监听生效。
 *
 * 防御：onCreate 与保存回调整体 try-catch —— 任何异常 Toast 展示而非闪退。
 */
class SettingsActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "SettingsActivity"
    }

    private lateinit var etSessionUrl: EditText
    private lateinit var tvDeviceId: TextView
    private lateinit var swWake: Switch
    private lateinit var sbThreshold: SeekBar
    private lateinit var tvThreshold: TextView
    private lateinit var swOverlay: Switch

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            onCreateSafe(savedInstanceState)
        } catch (t: Throwable) {
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
        setContentView(R.layout.activity_settings)
        setTitle(R.string.settings_title)

        etSessionUrl = findViewById(R.id.etSessionUrl)
        tvDeviceId = findViewById(R.id.tvDeviceId)
        swWake = findViewById(R.id.swWake)
        sbThreshold = findViewById(R.id.sbThreshold)
        tvThreshold = findViewById(R.id.tvThreshold)
        swOverlay = findViewById(R.id.swOverlay)

        // 回填当前配置
        etSessionUrl.setText(VoiceConfig.sessionBaseUrl(this))
        tvDeviceId.text = VoiceConfig.deviceId(this)
        swWake.isChecked = VoiceConfig.wakeEnabled(this)
        swOverlay.isChecked = VoiceConfig.overlayEnabled(this)

        // SeekBar 0..40 → 0.10..0.50
        val threshold = VoiceConfig.threshold(this)
        sbThreshold.progress = ((threshold - VoiceConfig.THRESHOLD_MIN) / 0.01f).toInt().coerceIn(0, 40)
        tvThreshold.text = getString(R.string.settings_threshold_label) + "：%.2f".format(threshold)
        sbThreshold.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                val value = VoiceConfig.THRESHOLD_MIN + progress * 0.01f
                tvThreshold.text = getString(R.string.settings_threshold_label) + "：%.2f".format(value)
            }

            override fun onStartTrackingTouch(seekBar: SeekBar?) {}
            override fun onStopTrackingTouch(seekBar: SeekBar?) {}
        })

        findViewById<Button>(R.id.btnSave).setOnClickListener {
            try {
                VoiceConfig.setSessionBaseUrl(this, etSessionUrl.text.toString())
                VoiceConfig.setWakeEnabled(this, swWake.isChecked)
                VoiceConfig.setThreshold(
                    this,
                    VoiceConfig.THRESHOLD_MIN + sbThreshold.progress * 0.01f
                )
                VoiceConfig.setOverlayEnabled(this, swOverlay.isChecked)
                Toast.makeText(this, R.string.saved, Toast.LENGTH_SHORT).show()
                finish()
            } catch (t: Throwable) {
                Log.e(TAG, "save crashed: ${t.message}", t)
                try {
                    Toast.makeText(
                        this,
                        getString(R.string.save_error_toast, t.message ?: t.javaClass.simpleName),
                        Toast.LENGTH_LONG
                    ).show()
                } catch (_: Throwable) {
                }
            }
        }
    }
}
