# ADR-013: 使用 TRTC 作为商业全双工媒体主链路

## Status: Accepted (2026-08-07)

## Background

单用户 Windows 桌面宠物与 Android 随身语音助手需要连续、低延迟、可打断的双向音频。裸 WebSocket PCM 无法在 MVP 周期内可靠承担移动网络抖动、AEC、重连和播放时钟；自建 SFU 会扩大部署和运维边界。当前商业发布仍缺 Android 扬声器连续两轮的真实证据。

## Decision

P0 唯一主媒体链路固定为：

```text
Android microphone -> TRTC Android uplink -> PC Node/Electron sidecar
-> bounded rtc_bridge -> MiniCPM-o/APM Realtime -> bounded DownlinkShaper
-> PC Node/Electron sidecar -> TRTC Android downlink -> Android speaker
```

Android 主页面、悬浮球、前台通知 action 均投递同一个会话 coordinator，并进入上述 TRTC 全双工路径。唤醒词与用户显式选择的半双工兼容模式属于 P1；P0 失败时不得自动切换并继续显示全双工状态。HTTP/WS 仅承担签发、控制和 localhost bridge，不作为商业主媒体平面。

## Consequences

正面后果：复用成熟 RTC 的 AEC、抖动控制和重连能力；三入口共享一个状态和证据模型；MVP 不引入自建 SFU。

负面后果：依赖 TRTC Android 与 Windows SDK 兼容矩阵；产生 RTC 服务成本；发布必须取得至少一台 Android 真机连续两轮、非零播放与 P95 300 ms 打断证据。

风险约束：Node/Electron SDK 候选版本和 Windows 注入签名未放行；历史 48 kHz 假设不是协议事实。

## Alternatives

- 裸 WebSocket PCM：拒绝作为 P0 主链路，时钟、AEC、抖动和重连风险不可接受。
- 自建 WebRTC SFU：拒绝，超出单用户 MVP 范围。
- 自动半双工降级：拒绝进入 P0；只能由用户在 P1 独立入口显式选择。

## Related ADRs

ADR-015、ADR-016、ADR-017。
