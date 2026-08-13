package com.jax.voice.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.jax.voice.MainActivity
import com.jax.voice.R

/**
 * 前台通知渲染（Task 6：服务只发送会话命令并渲染 VoiceSessionModel，通知是渲染的一部分）。
 * 通知通道与 ACTION 常量语义不删除；ACTION_TALK 等入口常量保留在 [VoiceForegroundService]。
 */
internal class VoiceServiceNotifications(private val service: Service) {

    companion object {
        const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "voice_listening"
    }

    /** 启动前台服务：创建通道 + 立即展示通知（Android 14+ 前台服务类型需按 API 分级传入） */
    fun startForegroundCompat() {
        val nm = service.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, service.getString(R.string.notif_channel_name), NotificationManager.IMPORTANCE_LOW).apply {
                description = service.getString(R.string.notif_channel_desc)
            }
        )
        val notification = buildNotification(service.getString(R.string.notif_title))
        ServiceCompat.startForeground(
            service,
            NOTIFICATION_ID,
            notification,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            } else {
                0
            }
        )
    }

    /** 刷新通知正文（标题由服务按 VoiceController 阶段映射后传入） */
    fun update(title: String) {
        val nm = service.getSystemService(NotificationManager::class.java) ?: return
        nm.notify(NOTIFICATION_ID, buildNotification(title))
    }

    private fun buildNotification(title: String): Notification {
        val pi = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        val openApp = PendingIntent.getActivity(
            service, 0, Intent(service, MainActivity::class.java), pi
        )
        val talk = PendingIntent.getService(
            service, 1,
            Intent(service, VoiceForegroundService::class.java).setAction(VoiceForegroundService.ACTION_TALK),
            pi
        )
        val pause = PendingIntent.getService(
            service, 2,
            Intent(service, VoiceForegroundService::class.java).setAction(VoiceForegroundService.ACTION_PAUSE),
            pi
        )
        val exit = PendingIntent.getService(
            service, 3,
            Intent(service, VoiceForegroundService::class.java).setAction(VoiceForegroundService.ACTION_STOP),
            pi
        )
        return NotificationCompat.Builder(service, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(service.getString(R.string.notif_text))
            .setSmallIcon(R.drawable.ic_stat_mic)
            .setOngoing(true)
            .setContentIntent(openApp)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(0, service.getString(R.string.notif_action_talk), talk)
            .addAction(0, service.getString(R.string.notif_action_pause), pause)
            .addAction(0, service.getString(R.string.notif_action_exit), exit)
            .build()
    }
}
