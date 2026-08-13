package com.jax.voice

import android.app.Application
import android.os.Build
import android.util.Log
import java.io.BufferedReader
import java.io.File
import java.io.FileReader
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 全局崩溃捕获（无线诊断）：
 * 未捕获异常 → 写崩溃栈 crash_log.txt **双写**：
 *   ① 应用私有 filesDir（App 必可读 —— MainActivity 弹窗展示用，用户无需 USB/文件管理器）
 *   ② getExternalFilesDir（Android 11+ 受保护，保留以便 adb/备份取用）
 * MainActivity 启动时读取私有 crash_log.txt 弹窗展示崩溃原因（按钮"知道了/分享"），展示后清空。
 * 写入后继续走系统默认处理（保持崩溃行为）。
 */
class JaxApp : Application() {

    companion object {
        const val CRASH_LOG_NAME = "crash_log.txt"
        /** 弹窗展示的堆栈行数上限（前 20 行：时间/设备/线程/异常头/栈顶帧） */
        const val CRASH_SUMMARY_LINES = 20
        private const val TAG = "JaxApp"
    }

    override fun onCreate() {
        super.onCreate()
        // v0.6.4 诊断日志初始化（无线定位手机端问题）
        try {
            com.jax.voice.util.DiagLog.init(this)
            com.jax.voice.util.DiagLog.log("App", "diag log initialized")
        } catch (_: Throwable) {
        }
        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                writeCrash(thread, throwable)
            } catch (_: Throwable) {
                // 捕获线程自身绝不能崩
            }
            defaultHandler?.uncaughtException(thread, throwable)
        }
    }

    /** 应用私有目录中的崩溃日志（App 必可读，弹窗展示用） */
    fun crashLogFile(): File = File(filesDir, CRASH_LOG_NAME)

    /**
     * 非致命异常也落日志（如 mic 采集线程捕获的 Error）：
     * 与未捕获崩溃同格式双写（私有 filesDir + 外部目录），下次启动弹窗可见。
     */
    fun logCrash(thread: Thread, throwable: Throwable) {
        try {
            writeCrash(thread, throwable)
        } catch (_: Throwable) {
        }
    }

    /** 外部目录中的崩溃日志（Android 11+ 受保护，文件管理器不可见；保留 adb/备份取用） */
    private fun externalCrashLogFile(): File? =
        getExternalFilesDir(null)?.let { File(it, CRASH_LOG_NAME) }

    /**
     * 读取崩溃摘要（头部信息 + 前 [CRASH_SUMMARY_LINES] 行堆栈）；无日志/读取失败返回 null。
     * 用 BufferedReader 提前截断，避免整文件读入（崩溃日志为追加式）。
     */
    fun readCrashSummary(): String? {
        val f = crashLogFile()
        if (!f.exists() || f.length() == 0L) return null
        return try {
            BufferedReader(FileReader(f)).use { reader ->
                val sb = StringBuilder()
                var count = 0
                while (count < CRASH_SUMMARY_LINES) {
                    val line = reader.readLine() ?: break
                    if (count > 0) sb.append('\n')
                    sb.append(line)
                    count++
                }
                sb.toString().ifBlank { null }
            }
        } catch (_: Throwable) {
            null
        }
    }

    /** 清空崩溃日志（弹窗展示后调用，避免每次启动重复弹窗） */
    fun clearCrashLog() {
        try {
            crashLogFile().delete()
        } catch (_: Throwable) {
        }
        try {
            externalCrashLogFile()?.delete()
        } catch (_: Throwable) {
        }
        try {
            CrashLogMirror(MediaStoreCrashLogMirrorStore(contentResolver)).clear()
        } catch (_: Throwable) {
        }
    }

    private fun writeCrash(thread: Thread, throwable: Throwable) {
        val sb = buildLog(thread, throwable)
        // 双写：① 应用私有目录（弹窗可达）② 外部目录（原行为，保留）
        FileWriter(crashLogFile(), true).use { it.append(sb) }
        val external = externalCrashLogFile()
        if (external != null) {
            FileWriter(external, true).use { it.append(sb) }
        }
        // ③ MediaStore Downloads 镜像（API 29+ 免权限）：文件管理器"下载/波斯猫/"直接可见
        mirrorToDownloads(sb)
        Log.e(TAG, "crash logged to ${crashLogFile().absolutePath}")
    }

    private fun buildLog(thread: Thread, throwable: Throwable): String {
        val ts = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
        val sb = StringBuilder()
        sb.append("\n===== crash @ ").append(ts).append(" =====\n")
        sb.append("device: ").append(Build.MANUFACTURER).append(' ').append(Build.MODEL)
            .append(" / SDK ").append(Build.VERSION.SDK_INT).append('\n')
        sb.append("thread: ").append(thread.name).append('\n')
        sb.append(Log.getStackTraceString(throwable)).append('\n')
        // 附全线程栈（帮助定位后台崩溃）
        val all = Thread.getAllStackTraces()
        for ((t, st) in all) {
            if (t === thread) continue
            sb.append("--- thread: ").append(t.name).append(" ---\n")
            for (frame in st) sb.append("\tat ").append(frame).append('\n')
        }
        return sb.toString()
    }

    /** MediaStore Downloads 镜像：稳定替换同一记录，避免每次崩溃产生重复文件。 */
    private fun mirrorToDownloads(content: String) {
        try {
            CrashLogMirror(MediaStoreCrashLogMirrorStore(contentResolver)).write(content)
        } catch (t: Throwable) {
            Log.e(TAG, "mirror downloads failed: ${t.message}")
        }
    }
}
