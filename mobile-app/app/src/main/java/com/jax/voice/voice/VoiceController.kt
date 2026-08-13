package com.jax.voice.voice

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * 全局状态总线：前台服务写、UI 读（spec §9）。
 *
 * Task 8：新增统一 [uiModel]（VoiceUiModel，10 个体验状态）供 UI/通知消费；
 * 旧的 [ui]（VoiceUiState 六态）保留兼容存量界面，逐步迁移。业务状态仍由
 * VoiceSessionCoordinator 串行裁决，此处只做聚合发布，不参与会话逻辑。
 */
object VoiceController {
    private val _ui = MutableStateFlow(VoiceUiState())
    val ui: StateFlow<VoiceUiState> = _ui.asStateFlow()

    private val _uiModel = MutableStateFlow(VoiceUiModel())
    val uiModel: StateFlow<VoiceUiModel> = _uiModel.asStateFlow()

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
        publishModel(_uiModel.value.copy(rms = rms))
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

    /** 发布统一体验状态（Task 8：UI/通知只消费聚合模型） */
    fun publishExperience(state: ExperienceState, session: VoiceSessionState? = null) {
        publishModel(
            _uiModel.value.copy(
                experience = state,
                session = session ?: _uiModel.value.session
            )
        )
    }

    fun publishModel(model: VoiceUiModel) {
        _uiModel.value = model
    }

    /** 进程被杀后由 MainActivity 重建默认值（服务停止时状态复位） */
    fun reset() {
        _ui.value = VoiceUiState()
        _uiModel.value = VoiceUiModel()
    }
}
