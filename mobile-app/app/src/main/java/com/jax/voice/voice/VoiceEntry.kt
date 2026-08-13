package com.jax.voice.voice

import android.content.Context
import android.content.Intent

/**
 * P0 三入口统一命令适配层（SPEC §4.2 / DESIGN-DETAIL §3 / Task 8）。
 *
 * 主页面「开始对话」、悬浮球轻触、前台通知「立即对话」都必须经
 * [startConversation] 投递同一个 `ACTION_TALK` 命令，服务端解析 source 后
 * 进入同一个 [VoiceSessionCoordinator.start]（TRTC 全双工路径）。
 * 任一入口失败不得影响另外两个（各入口独立组装 Intent）。
 *
 * P0 路径不调用 WakeWordEngine；失败分支不自动进入半双工兼容模式（P1 边界）。
 * 注意：JVM 单测下 android.jar stub 不保存 Intent 状态，因此 [lastStartCommand]
 * 记录最近一次统一命令供测试/埋点断言，不依赖 Intent 读写。
 */
object VoiceEntry {

    const val EXTRA_SOURCE = "com.jax.voice.extra.START_SOURCE"

    /** 统一命令快照（action + 入口 source；测试与埋点使用） */
    data class StartCommand(val action: String, val source: String)

    @Volatile
    var lastStartCommand: StartCommand? = null

    /** 服务启动器（测试注入：替换为记录器断言三入口收敛同一命令） */
    @Volatile
    var serviceLauncher: (Context, Intent) -> Unit = { context, intent ->
        context.startForegroundService(intent)
    }

    /**
     * 统一发起会话。source 标识入口：main / overlay / notification（Task 8 验收要求
     * 三入口独立进入同一个 coordinator.Start；source 用于诊断与埋点）。
     */
    fun startConversation(context: Context, source: String) {
        val intent = Intent(context, VoiceForegroundService::class.java)
        intent.action = VoiceForegroundService.ACTION_TALK // 勿链式：stub 下 setAction 返回 null
        intent.putExtra(EXTRA_SOURCE, source)
        lastStartCommand = StartCommand(VoiceForegroundService.ACTION_TALK, source)
        serviceLauncher(context, intent)
    }

    /** 从 intent 解析入口 source（通知按钮无 extra 时回落默认值） */
    fun resolveSource(intent: Intent?, default: String): String =
        intent?.getStringExtra(EXTRA_SOURCE)?.takeIf { it.isNotBlank() } ?: default
}
