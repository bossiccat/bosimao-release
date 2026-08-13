package com.jax.voice.util

import android.content.Context
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 手机端诊断日志（v0.6.4，无线定位"已连接但听不到声音"）：
 * 关键事件落盘 filesDir/diag_log.txt（App 私有目录，必可读），
 * MainActivity 长按连接状态区弹窗查看 + 复制/分享，用户可发回给开发。
 *
 * 记录内容（由调用方埋点）：进房/退房/回调（onEnterRoom/onFirstAudioFrame/
 * onRemoteAudioStatusUpdated/音量）/mute-unmute 动作/错误。
 */
object DiagLog {

    private const val TAG = "DiagLog"
    private const val MAX_LINES = 300

    @Volatile
    private var file: File? = null

    private val fmt = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    fun init(ctx: Context) {
        if (file == null) {
            file = File(ctx.filesDir, "diag_log.txt")
        }
    }

    @Synchronized
    fun log(tag: String, msg: String) {
        val f = file ?: return
        try {
            val line = "[${fmt.format(Date())}] [$tag] $msg\n"
            f.appendText(line)
            trimIfNeeded(f)
        } catch (t: Throwable) {
            Log.w(TAG, "diag write failed: ${t.message}")
        }
    }

    private fun trimIfNeeded(f: File) {
        try {
            val lines = f.readLines()
            if (lines.size > MAX_LINES) {
                f.writeText(lines.takeLast(MAX_LINES / 2).joinToString("\n") + "\n")
            }
        } catch (_: Throwable) {
            // 裁剪失败不影响主流程
        }
    }

    fun readRecent(maxLines: Int = 100): String {
        val f = file ?: return "(诊断日志未初始化)"
        return try {
            f.readLines().takeLast(maxLines).joinToString("\n")
        } catch (t: Throwable) {
            "读取失败: ${t.message}"
        }
    }

    fun clear() {
        try {
            file?.writeText("")
        } catch (_: Throwable) {
        }
    }
}
