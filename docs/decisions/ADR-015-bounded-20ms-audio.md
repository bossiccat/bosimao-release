# ADR-015: 使用固定 20 ms PCM 与有界实时音频缓冲

## Status: Accepted (2026-08-07)

## Background

当前 bridge 和下行整形路径存在无界队列与按回调块切片风险。网络或模型变慢会积累陈旧音频；不足一帧的尾部若直接发送会破坏实时节拍。音频回调若阻塞 I/O 或等待锁，会进一步放大抖动。

## Decision

模型侧契约固定为 PCM16LE、16000 Hz、单声道、20 ms、640 bytes。`rtc_bridge` 跨输入块保存 residue，仅输出完整帧；会话尾部按显式配置补零或丢弃并记指标，禁止发送变长帧。

采集、bridge 上行、APM 下行、sidecar 下行和播放队列都必须同时限制 `max_frames`、`max_bytes`、`max_frame_age_ms`。队列项携带 generation、创建时间和字节数。过载采用丢旧保新或终止会话，并记录 depth、high-watermark、drops、backpressure events。`pause/flush/interrupt` 幂等并使旧 generation 帧失效。音频回调只做非阻塞投递。

Windows TRTC 注入的真实采样率、帧长、字段和调用签名由实际 Node/Electron SDK 包、官方 Windows 契约和真机证据锁定；48 kHz 仍是未验证假设，不得写成正式事实。

## Consequences

正面后果：内存和延迟有明确上限；打断后不会复播迟到旧音频；压力测试可用机械指标验收。

负面后果：过载时允许可观测丢帧；需要跨块 residue、generation flush 和队列容量配置；TRTC adapter 可能需要确定性重采样。

## Alternatives

- 无界 `asyncio.Queue`：拒绝，会积累陈旧音频并可能耗尽内存。
- 直接按 SDK 回调边界发送：拒绝，不能保证 640-byte 固定帧。
- 提前锁定 48 kHz 注入：拒绝，缺实际 SDK 与真机证据。

## Related ADRs

ADR-013、ADR-016、ADR-017。
