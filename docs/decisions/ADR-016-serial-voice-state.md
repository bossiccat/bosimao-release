# ADR-016: 使用串行语音生命周期与聚合 UI 模型

## Status: Accepted (2026-08-07)

## Background

签发、进房和退房曾由 Job、布尔量和旁路线程协调，取消可能等待不存在的退房回调，旧 session 的迟到事件也可能重入新会话。UI 以多个布尔值拼装状态会让媒体订阅和视觉状态耦合。

## Decision

唯一会话生命周期为：

```text
IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE
```

所有 start、stop、sign、enter、exit、cancel、reconnect、timeout 和 failure 事件由单一 coordinator/dispatcher 顺序消费。旧 generation 事件丢弃；取消、退出、重连、pause、flush、interrupt 幂等；任一失败路径在保留错误原因后回到 IDLE。桌面监控状态独立，不得替代语音 IDLE。

Android 与 Windows 只消费聚合 `VoiceUiModel`。体验态限定为 idle、requesting_permission、connecting、listening、endpointing、thinking、speaking、interrupted、recovering、error。禁止用 `isListening`、`inCall`、`rtcExiting` 等业务布尔拼装 UI。远端停止说话只驱动 UI，不得静音长期播放订阅。

用户开口或点击到 Android 实际停止播放的 P95 不超过 300 ms；随后进入 listening，旧 generation 下行禁止复播。

## Consequences

正面后果：取消、超时、重进和快速点击有确定语义；三入口复用一个 coordinator；UI 与媒体订阅解耦。

负面后果：需要迁移现有布尔协调逻辑；所有 SDK 回调必须转换为状态事件；测试需覆盖非法转换和迟到事件。

## Alternatives

- 多布尔值加锁：拒绝，状态组合不可穷举且易产生永久退出锁。
- 旁路线程超时后直接重进：拒绝，会绕过状态机并造成竞态。
- 将桌面 MONITORING 作为语音空闲态：拒绝，两者是正交子系统。

## Related ADRs

ADR-013、ADR-015。
