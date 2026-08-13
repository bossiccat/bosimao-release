package com.jax.voice.voice

/**
 * 会话生命周期（SPEC §4.2 双层状态契约第一层 / ADR-016）。
 *
 * 唯一合法流转：
 * `IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE`
 * 任何结束路径（取消/超时/失败/退出回调）都必须收敛回 IDLE，禁止永久锁。
 */
enum class VoiceSessionState {
    IDLE, SIGNING, ENTERING, IN_ROOM, EXITING
}

/**
 * 聚合会话模型：UI 与服务只消费本模型（SPEC §4.2：禁止 inCall/rtcExiting 等并行业务布尔拼装）。
 *
 * @param generation 会话代数：每次接受 Start 递增；旧 generation 迟到事件由 coordinator 丢弃。
 * @param sessionId  服务端会话标识（缺省回落 roomId；IDLE 复位为 null）。
 * @param error      分类错误原因（取消为 null；超时/失败带原因，UI 按 SPEC §6 分类展示）。
 */
data class VoiceSessionModel(
    val state: VoiceSessionState = VoiceSessionState.IDLE,
    val generation: Long = 0L,
    val sessionId: String? = null,
    val error: String? = null
)

/** TRTC 进房凭证（与 net.VoiceSessionApi 解耦的最小数据，供 coordinator 效果注入） */
data class VoiceSessionInfo(
    val roomId: String,
    val userId: String,
    val userSig: String,
    val sdkAppId: Int,
    val sessionId: String? = null
)
