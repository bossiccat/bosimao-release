package com.jax.voice.voice

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * 全局状态总线：前台服务写、UI 读。
 * 与后端 EventBus（EVT_PET_STATE）同语义 —— 手机悬浮窗与通知同源同步（spec §9）。
 */
object VoiceController {
    private val _ui = MutableStateFlow(VoiceUiState())
    val ui: StateFlow<VoiceUiState> = _ui.asStateFlow()

    fun update(transform: (VoiceUiState) -> VoiceUiState) {
        _ui.update(transform)
    }

    fun setPhase(phase: VoicePhase) {
        _ui.update { it.copy(phase = phase) }
    }

    fun setConnection(state: ConnectionState) {
        _ui.update { it.copy(connection = state) }
    }

    /** 记录连接/配对失败原因（v0.4.7 诊断；空串清空） */
    fun setLastError(msg: String) {
        _ui.update { it.copy(lastError = msg) }
    }

    fun setService(state: ServiceState) {
        _ui.update { it.copy(service = state) }
    }

    fun setRms(rms: Float) {
        _ui.update { it.copy(rms = rms) }
    }

    fun onWake(keyword: String) {
        _ui.update {
            it.copy(
                phase = VoicePhase.LISTENING,
                wakeCount = it.wakeCount + 1,
                lastKeyword = keyword
            )
        }
    }

    /** 进程被杀后由 MainActivity 重建默认值（服务停止时状态复位） */
    fun reset() {
        _ui.value = VoiceUiState()
    }
}
