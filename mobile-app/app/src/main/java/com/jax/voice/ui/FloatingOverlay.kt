package com.jax.voice.ui

import android.content.Context
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import com.jax.voice.R
import com.jax.voice.voice.VoiceEntry
import com.jax.voice.voice.VoicePhase
import kotlin.math.abs

/**
 * 悬浮窗（spec §4.5）：常驻小圆球，状态色 = 六态语义色。
 * 交互（spec §5.3 兜底）：轻触 = 唤醒（发 ACTION_TALK）；长按拖动；再长按 = 隐藏。
 */
class FloatingOverlay(private val context: Context) {

    companion object {
        private const val TAG = "FloatingOverlay"
        private const val BALL_SIZE_DP = 56
    }

    private var windowManager: WindowManager? = null
    private var ball: View? = null
    private var params: WindowManager.LayoutParams? = null

    private val density = context.resources.displayMetrics.density
    private var lastX = 0f
    private var lastY = 0f
    private var dragging = false
    private var touchDownTime = 0L

    fun isShowing(): Boolean = ball != null

    /** 需要 SYSTEM_ALERT_WINDOW 权限（MainActivity 引导，不静默申请） */
    fun show() {
        try {
            showInner()
        } catch (t: Throwable) {
            // 防御：addView 抛 BadTokenException 等 → 复位状态，绝不崩
            Log.e(TAG, "overlay show failed: ${t.message}", t)
            ball = null
            windowManager = null
            params = null
        }
    }

    private fun showInner() {
        if (ball != null) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(context)) {
            Log.w(TAG, "overlay permission not granted")
            return
        }
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val size = (BALL_SIZE_DP * density).toInt()
        // v0.4.5：悬浮球 = 波斯猫 logo（内层）+ 状态色光环（外层半透明圆，六态可见）
        val ballView = FrameLayout(context).apply {
            layoutParams = FrameLayout.LayoutParams(size, size)
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(phaseColorAlpha(context, R.color.state_monitoring))
            }
            addView(android.widget.ImageView(context).apply {
                layoutParams = FrameLayout.LayoutParams((size * 3 / 5), (size * 3 / 5), Gravity.CENTER)
                setImageResource(R.drawable.ic_button_bosimao)
                scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
            })
        }
        val lp = WindowManager.LayoutParams(
            size,
            size,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            } else {
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE
            },
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 24
            y = 160
        }
        ballView.setOnTouchListener { v, event -> handleTouch(v, event) }
        wm.addView(ballView, lp)
        ball = ballView
        windowManager = wm
        params = lp
    }

    /** 状态色光环：原色 + 32% 透明度（半透明，logo 保持清晰） */
    private fun phaseColorAlpha(ctx: android.content.Context, colorRes: Int): Int =
        ctx.getColor(colorRes) and 0x00FFFFFF or 0x52000000.toInt()

    fun updatePhase(phase: VoicePhase) {
        val v = ball ?: return
        val colorRes = when (phase) {
            VoicePhase.IDLE -> R.color.state_idle
            VoicePhase.MONITORING -> R.color.state_monitoring
            VoicePhase.LISTENING -> R.color.state_listening
            VoicePhase.THINKING -> R.color.state_thinking
            VoicePhase.SPEAKING -> R.color.state_speaking
            VoicePhase.ALERTING -> R.color.state_alerting
        }
        // 只更新外层状态光环（logo 保持清晰）
        (v.background as? GradientDrawable)?.setColor(phaseColorAlpha(v.context, colorRes))
    }

    private fun handleTouch(v: View, event: MotionEvent): Boolean {
        val wm = windowManager ?: return false
        val lp = params ?: return false
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                lastX = event.rawX
                lastY = event.rawY
                dragging = false
                touchDownTime = System.currentTimeMillis()
                return true
            }

            MotionEvent.ACTION_MOVE -> {
                val dx = event.rawX - lastX
                val dy = event.rawY - lastY
                if (abs(dx) > 4 || abs(dy) > 4) {
                    dragging = true
                    lp.x += dx.toInt()
                    lp.y += dy.toInt()
                    wm.updateViewLayout(v, lp)
                    lastX = event.rawX
                    lastY = event.rawY
                }
                return true
            }

            MotionEvent.ACTION_UP -> {
                if (!dragging) {
                    val elapsed = System.currentTimeMillis() - touchDownTime
                    if (elapsed < 400) {
                        // 轻触 = 发起对话（spec §5.3 交互兜底；Task 8 统一 startConversation 命令）
                        try {
                            VoiceEntry.startConversation(context, "overlay")
                        } catch (e: Exception) {
                            // Android 14 后台启动 mic 前台服务受限（spec §11-1）：引导打开 App
                            Log.w(TAG, "start foreground service failed: ${e.message}")
                        }
                    } else if (elapsed >= 600) {
                        // 长按 = 隐藏悬浮窗
                        hide()
                    }
                }
                return true
            }
        }
        return false
    }

    fun hide() {
        try {
            val v = ball ?: return
            // 防御：removeView 已移除的视图抛 IllegalArgumentException（边缘闪退）——捕获后复位即可
            try {
                windowManager?.removeView(v)
            } catch (t: Throwable) {
                Log.e(TAG, "removeView failed: ${t.message}")
            }
            ball = null
            windowManager = null
            params = null
        } catch (t: Throwable) {
            Log.e(TAG, "overlay hide failed: ${t.message}", t)
            ball = null
            windowManager = null
            params = null
        }
    }
}
