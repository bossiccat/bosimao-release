package com.jax.voice.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.util.Log
import android.view.View
import com.jax.voice.R

/**
 * 语音波形（spec §4.5）：Canvas 自绘，不引图表库。
 * 将最近 N 帧 RMS 画成居中竖条；M1 为占位展示（数据源 = VoiceController.ui.rms）。
 */
class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    companion object {
        private const val TAG = "WaveformView"
        private const val BAR_COUNT = 32
    }

    private val bars = FloatArray(BAR_COUNT)
    private var head = 0

    /** 防御：getColor 资源缺失/异常 → 回退系统色，绝不因画笔初始化崩溃 */
    private val paint: Paint = try {
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = context.getColor(R.color.jax_accent)
            strokeCap = Paint.Cap.ROUND
        }
    } catch (t: Throwable) {
        Log.e(TAG, "paint init failed: ${t.message}")
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.GRAY
            strokeCap = Paint.Cap.ROUND
        }
    }

    fun pushRms(rms: Float) {
        val v = (rms * 4f).coerceIn(0.05f, 1f)
        bars[head] = v
        head = (head + 1) % BAR_COUNT
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        val gap = 4f * resources.displayMetrics.density
        val barWidth = (w - gap * (BAR_COUNT - 1)) / BAR_COUNT
        val midY = h / 2f
        paint.strokeWidth = barWidth * 0.6f

        for (i in 0 until BAR_COUNT) {
            val v = bars[(head + i) % BAR_COUNT]
            val barH = (h * 0.9f * v).coerceAtLeast(2f)
            val x = i * (barWidth + gap) + barWidth / 2f
            canvas.drawLine(x, midY - barH / 2f, x, midY + barH / 2f, paint)
        }
    }
}
