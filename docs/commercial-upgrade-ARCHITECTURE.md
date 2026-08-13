# Commercial Upgrade Architecture Baseline

## 1. 范围、结论与约束

目标是单用户 Windows 常驻桌面宠物 AI + Android 语音端，主体验为可打断的原生全双工语音。本文是 Phase 1 架构与技术选型基线，只约束架构、协议、安全、版本和交付门禁，不修改业务代码。

当前独立审计结论为 **FAIL**。仓库已有代码路径不能替代生产能力证据。至少四类缺口阻断放行：协议背压、签发鉴权、单实例串行守护、构建与 sidecar 锁定。Android microphone 前台服务声明也不能替代 Android 14+ 真机后台/锁屏证据。

功能图标统一锁定 `lucide-react@0.469.0`，全项目只使用该 SVG 图标库，禁止 emoji 作为功能图标和混用其他图标库；不采用紫色到粉色渐变方案。

## 2. 选型对比

评分按学习成本、生态成熟度、部署成本、团队熟悉度为高权重，扩展性为低权重，满分 5 分。评分是本轮独立推荐，不把仓库历史 ADR 当作预设结论。

| 领域 | 方案 A | 方案 B | 方案 C | 结论 |
|---|---|---|---|---|
| Windows 宿主 | Tauri 2 + React 18 + Rust sidecar，4.5 | Electron + React，4.0 | WinUI 3 + C#，3.0 | 采用 Tauri，补齐 sidecar、权限、单实例、托盘和自启 |
| Android | 原生 Kotlin + TRTC，4.5 | Flutter + 原生 RTC bridge，3.0 | React Native + 原生音频桥，2.5 | 采用原生 Kotlin + TRTC，减少后台音频桥复杂度 |
| 媒体平面 | TRTC，4.5 | 自建 WebSocket PCM/Opus，2.5 | 自建 WebRTC SFU，3.0 | TRTC 主媒体平面；WS 仅控制、relay 或降级 |
| AI/控制面 | FastAPI 单体 + bridge，4.5 | NestJS，3.5 | CloudBase 云函数，3.5 | 保留 FastAPI；长连接 bridge 不放入短生命周期函数 |
| MVP 本地数据 | SQLite，4.5 | PostgreSQL，4.0 | Redis，3.0 | 单用户只用 SQLite；PG/Redis 不进入本轮 |

## 3. 推荐架构与真实媒体拓扑

采用成熟 RTC 主链路，HTTP/WS 半双工作为明确降级路径。TRTC 负责手机与 PC 的实时媒体，不让自建 PCM WS 承担主链路的时钟、AEC、抖动和播放职责。

```text
表现层
  Windows Tauri + React/XState       Android Activity/Notification/Overlay
              |                                      |
应用层
  PC VoiceCoordinator                 Android VoiceSessionCoordinator
              |                                      |
领域层
  会话状态机 | 20ms PCM residue/buffer | 背压策略 | session metrics
              |                                      |
基础设施层
  FastAPI sign/control | TRTC adapter | rtc_bridge | APM adapter | SQLite
```

真实主链路：

```text
Android mic
  -> TRTC mobile uplink
  -> PC sidecar TRTC
  -> bounded rtc_bridge (固定 20ms PCM 跨块缓存、限长/限字节队列)
  -> APM / MiniCPM-o Realtime

APM downlink
  -> bounded DownlinkShaper (拆为固定 20ms 帧并按消费时长节拍)
  -> PC sidecar TRTC
  -> Android speaker
```

控制面：`POST /api/v1/voice/session` 或 `/session/sign` 先完成设备/sidecar 身份校验、nonce 防重放和限流，再签发短期 TRTC userSig。`session_id` 必须贯穿签发日志、TRTC room、sidecar、rtc_bridge、Android 服务日志和指标。

组件边界：

- **Windows Tauri**：桌面窗口、托盘、单实例、开机自启、sidecar 生命周期和 UI 状态展示。
- **Android**：麦克风采集、扬声器播放、权限、前后台/锁屏、音频焦点、蓝牙和前台服务生命周期。
- **FastAPI sign/control**：设备凭证、sidecar 凭证、短期 userSig 签发、session 绑定、nonce、限流和状态查询。
- **TRTC**：房间、实时上下行、AEC、抖动控制、重连及音频事件。
- **PC sidecar**：TRTC SDK 运行时与平台包，不持有业务状态；由 Tauri 以受限 capability 启停。
- **rtc_bridge**：格式转换、固定帧、跨块 residue、队列背压、取消与生命周期事件，不直接持有 UI 状态。
- **APM/MiniCPM-o**：模型推理、VAD/turn 和模型侧音频适配，不负责 RTC 房间生命周期。

## 4. 关键设计

### 4.1 音频与协议契约

TRTC SDK 最终版本以官方契约、官方 Android/Windows 兼容矩阵、干净安装和真实设备验证共同锁定。当前仓库已精确使用 `com.tencent.liteav:LiteAVSDK_TRTC:13.4.0.20477`，但不能仅凭本地可运行证明最终兼容性。

模型侧契约固定为 16 kHz、单声道、PCM16、20 ms、640 bytes；`rtc_bridge` 必须跨块保留 residue，禁止按每次回调边界直接截断。TRTC sidecar 适配的采样率、帧长和注入接口必须以该 SDK 的官方实际签名与兼容矩阵为准，不能继续沿用未经验证的 Electron 48 kHz 假设。下行 `DownlinkShaper` 必须有界、按 20 ms 节拍发送；不足一帧的尾部必须按协议明确补齐或丢弃，不能静默产生变长帧。

音频回调只投递事件，禁止阻塞 I/O、等待锁或同步 join。VAD/远端音频状态驱动 turn；用户开口立即取消或衰减 TTS，实现 barge-in。

### 4.2 串行会话生命周期

统一语音会话状态机为：

`IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE`

异常路径回到 `IDLE` 并保留错误原因。桌面监控保持独立子系统状态，不得用 `MONITORING` 代替语音会话 `IDLE`，也不得让截图调度状态驱动语音进退房。

体验状态统一使用 `VoiceUiModel` 枚举：`idle / requesting_permission / connecting / listening / endpointing / thinking / speaking / interrupted / recovering / error`。两套状态正交：会话处于 `IN_ROOM` 时可在 `listening/endpointing/thinking/speaking/interrupted/recovering` 间切换；退出时体验状态必须回到 `idle` 或进入 `error`。

所有 `start/stop/sign/enter/exit/cancel/reconnect` 事件必须在单一 coordinator/dispatcher 顺序执行。旧 session 的迟到事件按 generation 丢弃。超时是状态事件，不能由旁路 daemon thread 直接重入新进房。取消、退出和重连必须幂等，单实例只允许一个活动会话和一个音频设备 owner。

发布预算：从用户开口或点击打断到 Android 实际停止播放，P95 必须不超过 300 ms。`pause/flush/interrupt` 对重复调用幂等，必须清除或失效化已排队下行帧，停止当前播放并回到 `listening`；迟到的旧 generation 音频不得重新播放。

### 4.3 生产安全与 fail-closed

生产环境禁止继承开发默认放宽项。`backend/app/voice/config.py` 中 `require_token=False`、`e2ee_enabled=False` 只能用于明确隔离的本地开发配置，不能进入生产构建或生产启动配置。

生产启动或会话建立必须在以下任一条件缺失时拒绝：设备凭证、PC sidecar credential、nonce/短期签名验证器、限流策略、TLS 端点、TRTC SDKAppID/SecretKey。实现可以选择启动即失败，或服务启动但对所有会话返回拒绝；不得自动降级为匿名 WS、客户端自报 `device_id` 或明文 relay。

安全契约：

- 设备注册生成不可逆存储的设备凭证元数据；Secret 只展示一次，不进日志、不进 SQLite 明文。
- 手机请求使用 `Authorization: Bearer <device credential>` 与 `X-Request-Nonce`；nonce 仅消费一次并设置短 TTL。
- PC sidecar 使用独立 service credential，不能复用手机设备凭证。
- userSig TTL 不超过 600 秒；session/user/device/room 绑定，过期、撤销和重放均拒绝。
- `/session`、`/session/sign`、WS 握手按 device/IP 限流，超限返回 `42901`。
- 全链路 TLS；中继只转发密文或受保护帧，不记录原始音频。

### 4.4 有界背压与单实例守护

采集、TRTC 上行、bridge 上行、APM 下行、sidecar 下行和播放各自使用有界队列或 ring buffer，同时设置 `max_frames` 与 `max_bytes`。满载时丢弃过期语音帧或可取消 TTS，记录 `queue_high_watermark`、`queue_drops`、`backpressure_events`，不能使用无界 `asyncio.Queue`。

看门狗监测状态无进展、队列持续满载、取消超时和音频线程心跳；超时执行幂等 teardown 并记录 `session_id/error_code`。Windows 重复启动必须复用已有实例或明确退出；Android 进退房不可通过布尔量与旁路线程竞态协调。

### 4.5 Tauri 现状与目标

当前 `C:\Users\Administrator\WorkBuddy\监视app\pet-ui\src-tauri\Cargo.toml:7-12` 只声明 Tauri 2.11.5、`tray-icon` 和 `image-png`，尚未闭合 sidecar/shell/autostart/capability。当前目标必须补齐：

- `externalBin` sidecar 清单，按 `x86_64-pc-windows-msvc`（以及实际发布架构）提供带 target triple 后缀的可执行文件。
- `tauri-plugin-shell` 仅允许执行指定 sidecar 和固定参数；capability 文件采用最小权限，禁止泛化 shell 执行。
- `tauri-plugin-autostart` 或等价 Windows 自启实现，提供用户可见的开关和卸载清理。
- `tauri-plugin-single-instance` 或等价单实例互斥；托盘菜单、隐藏/恢复窗口、退出和 sidecar teardown 必须可验证。
- sidecar 崩溃重启上限、退出超时和日志脱敏；sidecar 不保存长期密钥。
- Windows 构建矩阵至少包含干净安装、开发构建、release 构建、sidecar 存在性/哈希校验和安装后托盘/自启/单实例验收。

### 4.6 精确版本与构建门禁

不得使用范围版本、`latest`、动态解析或未提交 lockfile。当前仓库基线：

- Windows UI：React `18.3.1`、TypeScript `5.6.3`、XState `5.19.0`、`lucide-react 0.469.0`、Tauri API `2.11.1`、Tauri CLI `2.11.4`。
- Tauri Rust：`tauri 2.11.5`。
- Android：compile/target SDK `35`、min SDK `26`、Kotlin plugin `1.9.24`、AGP `8.6.1`、JVM `17`、TRTC `13.4.0.20477`、OkHttp `4.12.0`、Coroutines `1.8.1`。
- 后端：Python `>=3.11`，FastAPI 依赖按干净环境解析后必须生成并提交锁定结果。

`package.json` 中现有 `^` 版本只视为审计证据，不是放行条件。最终版本必须经官方兼容矩阵、干净安装、依赖哈希校验、Windows/Android 构建和真机音频冒烟后锁定。CI 必须阻断范围版本、缺失 lockfile、sidecar 缺失、未认证签发、协议漂移及兼容矩阵未通过。

### 4.7 可观测性与数据最小化

所有日志和指标关联 `session_id`、`turn_id`、platform、device、state、error_code。至少记录：

- `up_frame_count`、`up_bytes`、`down_frame_count`、`down_bytes`
- `first_remote_audio_ts`、`first_nonzero_playback_ts`
- `queue_depth`、`queue_high_watermark`、`queue_drops`、`backpressure_events`
- `reconnects`、`enter_latency_ms`、`exit_latency_ms`、`error_code`

禁止记录原始音频、截图、代码内容、完整敏感文本和长期设备凭据。仅保留调试所需的摘要、延迟、队列和首帧/首音指标，设置 TTL、访问审计和脱敏策略。

### 4.8 本地隐私控制与数据权利

转写默认仅在内存中用于当轮 UI 展示，会话结束即清除，不持久化。只有用户显式开启“本地保存转写”后，才允许使用 Windows DPAPI 或等价 OS-bound key 在 SQLite 中加密保存；必须提供单条/全部删除和 JSON/UTF-8 文本导出，导出前由用户选择目标路径。原始音频、截图和代码内容始终不保存。

诊断导出只包含脱敏后的 session 事件、版本、状态转换、错误码、延迟、frame/byte 和队列指标；移除 token、userSig、credential、nonce、原始文本、文件路径中的用户名和设备可识别信息。诊断包生成后必须由用户显式确认导出。

以下开关进入 `settings` 并通过本地 service 接口立即生效：

| 本地接口 | 设置键 | 立即生效语义 |
|---|---|---|
| `LocalPrivacyService.setCloudProcessingEnabled(enabled)` | `privacy.cloud_processing_enabled` | 关闭时拒绝新 APM/第三方云端会话；活动云会话立即 interrupt、flush、exit，回本地 `idle` |
| `LocalPrivacyService.setMicrophoneEnabled(enabled)` | `privacy.microphone_enabled` | 关闭时立即停止采集、清空上行队列并释放 microphone owner；新会话进入 `requesting_permission` 或拒绝 |
| `LocalPrivacyService.setBackgroundConversationEnabled(enabled)` | `privacy.background_conversation_enabled` | 关闭时 App 进入后台/锁屏立即 pause、flush 并退出房间；不得保持 microphone FGS 对话 |
| `LocalPrivacyService.setDesktopCaptureEnabled(enabled)` | `privacy.desktop_capture_enabled` | 关闭时独立桌面监控子系统立即停止 WGC 调度并释放帧；不影响语音会话状态机 |
| `LocalTranscriptService.setPersistenceEnabled(enabled)` | `privacy.transcript_persistence_enabled` | 默认 false；关闭时停止后续写入，是否删除既有记录由用户单独确认 |
| `LocalTranscriptService.delete(transcript_id?)` | 无 | 删除指定或全部本地加密转写并写审计，不保留正文副本 |
| `LocalTranscriptService.export(format, destination)` | 无 | 仅导出用户选择的本地加密转写解密结果，不上传第三方 |
| `LocalDiagnosticsService.exportRedacted(destination)` | 无 | 仅导出脱敏诊断字段，生成前预览范围并写审计 |

所有 setter 返回 `{applied_at, effective_value, action_result}`；写 SQLite 与运行时动作在同一 service 编排，动作失败时回滚设置值并返回明确错误，禁止 UI 显示已关闭但底层仍采集或上传。

## 5. API 契约基线

所有 HTTP 端点带 `/api/v1/` 前缀，统一响应：`{"code": 0, "data": {}, "message": ""}`。认证头为 `Authorization: Bearer <credential>`。

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/v1/voice/devices/register` | `{device_name, platform, nonce}` | `{device_id, credential_id, expires_at}`；Secret 只一次展示 |
| GET | `/api/v1/voice/devices` | Bearer 本机 owner credential | `{items:[{device_id,device_name,platform,status,last_seen_at,created_at}],total}` |
| POST | `/api/v1/voice/devices/{device_id}/revoke` | Bearer 本机 owner credential；`X-Request-Nonce`；`{reason}` | `{device_id,status:"revoked",revoked_at,terminated_session_ids}` |
| POST | `/api/v1/voice/session` | Bearer 设备凭证；`X-Request-Nonce`；`{device_id}` | `{session_id, room_id, user_id, sdk_app_id, user_sig, expires_at, scene}` |
| POST | `/api/v1/voice/session/sign` | Bearer sidecar credential；`X-Request-Nonce`；`{device_id,user_id}` | 同上，签名主体为 sidecar user_id |
| GET | `/api/v1/voice/status` | Bearer credential | `{session_id,state,up_frame_count,up_bytes,down_frame_count,down_bytes,first_remote_audio_ts,first_nonzero_playback_ts,queue_depth,queue_drops}` |
| WS | `/api/v1/voice/stream` | 首帧 hello `{session_id,device_id,nonce,protocol_version,audio_format}`；二进制固定 20 ms PCM16LE 16 kHz | 控制帧 `ready/session_state/heartbeat/pong/transcript/error/reply_done`；二进制固定音频帧 |

控制帧允许 `heartbeat`、`interrupt`、`pause`、`resume`、`close`。非法顺序返回 `40901 state_conflict`，不静默接受。

设备撤销是强一致安全动作：成功后该设备 credential 立即拒绝，未过期 userSig 进入服务端撤销名单，活动 session 主动终止并写入审计事件；同一 device 的后续 session/sign/WS 握手全部返回 `40103 credential_revoked`。撤销请求自身按 nonce 幂等，重复撤销返回当前 revoked 状态。

错误码：

- `40001` `invalid_device_or_user`
- `40101` `auth_failed`
- `40102` `nonce_replay`
- `40103` `credential_revoked`
- `40401` `device_not_found`
- `40801` `handshake_timeout`
- `40901` `state_conflict`
- `41301` `queue_overflow`
- `42901` `rate_limited`
- `50300` `credential_unavailable`
- `50401` `upstream_timeout`

## 6. SQLite MVP 数据范围

单用户 MVP 只保存本地设置与最小审计元数据，不引入 PostgreSQL 或 Redis：

```text
settings
- id, key, value_encrypted, created_at, updated_at
- keys: privacy.cloud_processing_enabled, privacy.microphone_enabled,
        privacy.background_conversation_enabled, privacy.desktop_capture_enabled,
        privacy.transcript_persistence_enabled

device_credentials
- id, device_id, device_name, platform, credential_hash, status,
  expires_at, last_seen_at, revoked_at, revoke_reason, created_at, updated_at
- index(device_id), index(status), index(revoked_at)

revoked_sessions
- id, session_id, device_id, user_sig_fingerprint, expires_at, revoked_at, reason,
  created_at, updated_at
- index(session_id), index(device_id), index(expires_at)

session_events
- id, session_id, device_id, event_type, state, error_code, metadata_json,
  created_at, updated_at
- index(session_id), index(device_id), index(created_at)

transcripts
- id, session_id, ciphertext, encryption_version, started_at, ended_at,
  created_at, updated_at
- only when privacy.transcript_persistence_enabled=true
- index(session_id), index(created_at)

privacy_audit_events
- id, action, subject_type, subject_id, result, metadata_redacted_json,
  created_at, updated_at
- index(action), index(created_at)
```

每表必须有 `id`、`created_at`、`updated_at`。SecretKey、设备 Secret 明文、原始音频、截图、代码内容和第三方 token/userSig 明文不得落库。`transcripts.ciphertext` 仅保存用户显式开启的本地加密转写；默认不创建正文记录。会话指标可按脱敏事件摘要写入 `session_events.metadata_json`，不建立独立远端分析库。删除转写后不得在审计表、备份或诊断包保留正文；审计只记录删除动作、数量、时间与结果。

## 7. 迁移顺序与端到端门禁

1. 补齐设备凭证、设备列表/撤销、sidecar credential、nonce、防重放、TLS 和限流；生产配置缺任一项即 fail-closed。
2. 将 TRTC/sidecar/bridge/APM 拓扑按本文件闭合，落实固定 20 ms 跨块缓存与有界队列。
3. 将 Android 与 PC 进退房改为 `IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE` 单 dispatcher 串行状态机，统一幂等取消和 generation 丢弃。
4. 落实 `VoiceUiModel` 全枚举与 P95 <=300 ms 打断预算，验证重复 `pause/flush/interrupt` 后回 `listening` 且无迟到音频复播。
5. 闭合 Tauri externalBin、shell capability、autostart、single-instance、托盘、sidecar watchdog 和 Windows 构建矩阵。
6. 实现本地隐私 service：云端、麦克风、后台对话、桌面捕获、转写持久化开关即时生效；转写删除/导出与脱敏诊断导出可验收。
7. 完成依赖精确锁定、干净安装、类型检查、lint、单测、压力测试和可重复打包。
8. 至少 1 台 Android 真机连续两轮全双工：按 `session_id` 提交上/下行 frame+byte、远端首帧、非零播放证据；再测打断、退出重进、锁屏/后台、暂停/恢复和设备撤销中断。
9. 完成 Windows 单实例、托盘、自启、sidecar 启停/崩溃重启和安装后验收，再进行灰度。

## 8. 明确不做

MVP 不做多租户、账号体系、云端历史语音存储、PostgreSQL、Redis 集群、独立向量库、自建 SFU、匿名生产 WS、自动操控被监控 agent、无限后台重试和未经验证的 Electron 音频注入主链路。

## 9. 不可行警告

认证、sidecar、官方契约/兼容矩阵、固定帧背压和真机 E2E 缺失时，不可宣称 GPTLive 级全双工。仅靠自建 WebSocket 难以在 MVP 周期内稳定实现 AEC、自然打断和可靠重连。Android 若继续用布尔量、同步等待或旁路线程协调 enter/exit，仍存在竞态和恢复卡死风险。

## 10. ADR 清单

- ADR-001：复用 TRTC 作为移动与 PC 的成熟媒体主链路；状态：Proposed。
- ADR-002：以官方契约、干净安装和 Android/Windows 兼容矩阵锁定 SDK 版本；状态：Proposed。
- ADR-003：设备凭证、sidecar credential、短时 userSig、nonce 和限流；生产 fail-closed；状态：Proposed。
- ADR-004：采用 `IDLE/SIGNING/ENTERING/IN_ROOM/EXITING` 串行语音会话状态机；桌面监控状态保持独立；状态：Proposed。
- ADR-005：采用 bounded rtc_bridge、固定 20 ms PCM residue 和 DownlinkShaper；状态：Proposed。
- ADR-006：采用 Tauri externalBin、最小 capability、single-instance、托盘、自启和 watchdog；状态：Proposed。
- ADR-007：HTTP/WS 半双工仅作降级，不作为主体验；状态：Proposed。
- ADR-008：采用 SQLite 最小数据集与 session metrics，禁止原始音频、截图、代码内容留存；状态：Proposed。
- ADR-009：统一使用 `lucide-react@0.469.0` SVG 图标库，禁止 emoji 功能图标；状态：Accepted。
- ADR-010：采用 `VoiceUiModel` 统一体验状态和 P95 <=300 ms 打断预算；状态：Proposed。
- ADR-011：转写默认不持久化，可选本地加密保存/删除/导出；隐私设置通过本地 service 即时生效；状态：Proposed。
- ADR-012：设备列表与撤销为安全控制面；撤销后 credential/userSig/session 立即拒绝或终止并审计；状态：Proposed。

## 11. RoleVerdict

```yaml
verdict: fail
blocking:
  - 违反项: 语音链路没有有界端到端背压
    证据: C:\Users\Administrator\WorkBuddy\监视app\backend\rtc_bridge\session.py:57,86-92; C:\Users\Administrator\WorkBuddy\监视app\backend\rtc_bridge\shaper.py:28,37-42
    期望: 上行/下行队列同时设置 max_frames 与 max_bytes，固定 20ms 跨块缓存，超限有丢帧或断开策略并记录 queue_depth/queue_drops/backpressure_events
  - 违反项: 生产鉴权可自动放宽为开发态
    证据: C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\config.py:57-64; C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\session.py:221-253
    期望: 缺设备凭证、sidecar credential、nonce/短期签名、TLS 或限流时拒绝启动或拒绝所有会话，禁止匿名生产 WS
  - 违反项: 签发接口未绑定设备身份且无 sidecar 独立认证契约
    证据: C:\Users\Administrator\WorkBuddy\监视app\backend\app\api\routes_voice.py:39-50,125-177
    期望: `/session` 使用设备 Bearer 凭证，`/session/sign` 仅接受 sidecar credential，nonce 仅消费一次，userSig TTL <=600s，按 device/IP 限流
  - 违反项: Android 进退房不是严格串行状态机
    证据: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\voice\VoiceForegroundService.kt:253-320; C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\net\RtcClient.kt:281-344,392-436
    期望: `IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE` 由单一 dispatcher 顺序执行，桌面 MONITORING 独立，超时转事件且不得旁路重入
  - 违反项: 完整 VoiceUiModel 与打断发布预算尚无实现证据
    证据: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\voice\VoiceState.kt:4-13,21-32; C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\net\RtcClient.kt:231-265
    期望: 体验态固定为 idle/requesting_permission/connecting/listening/endpointing/thinking/speaking/interrupted/recovering/error；用户开口或点击到停止播放 P95 <=300ms，pause/flush/interrupt 幂等并回 listening
  - 违反项: 设备列表、撤销与撤销传播尚未实现
    证据: C:\Users\Administrator\WorkBuddy\监视app\backend\app\api\routes_voice.py:39-50,125-177
    期望: 实现 GET /api/v1/voice/devices 与 POST /api/v1/voice/devices/{device_id}/revoke；撤销后 credential/userSig/session 立即拒绝或终止并写审计
  - 违反项: 本地隐私控制和转写/诊断数据权利尚无实现证据
    证据: C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\session.py:82-88,146-160; C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\config.py:45-63
    期望: 转写默认不持久化；可选仅本地加密保存、删除、导出；诊断脱敏导出；云端、麦克风、后台对话、桌面捕获四类开关经本地 service 即时生效
  - 违反项: Tauri sidecar 与 Windows 常驻能力未闭合
    证据: C:\Users\Administrator\WorkBuddy\监视app\pet-ui\src-tauri\Cargo.toml:7-12
    期望: externalBin、shell 最小 capability、autostart、single-instance、托盘、sidecar watchdog、x86_64-pc-windows-msvc 构建矩阵和安装后验收齐全
  - 违反项: 依赖使用范围版本且缺少可审计的精确锁定门禁
    证据: C:\Users\Administrator\WorkBuddy\监视app\pet-ui\package.json:12-26
    期望: npm/cargo/Gradle 版本与 lockfile 固定，最终版本经官方兼容矩阵、干净安装和真实设备冒烟验证
  - 违反项: Android 真机全双工能力无连续两轮证据
    证据: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\AndroidManifest.xml:4-21,46-54; C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\build.gradle.kts:6-20,54-74
    期望: 至少 1 台 Android 真机连续两轮全双工，提交 session_id、上/下行 frame+byte、远端首帧、非零播放、打断、锁屏/后台、暂停/恢复证据
advisory:
  - 建议项: 保留 Tauri + React、原生 Kotlin + TRTC、FastAPI + bridge、SQLite MVP
    理由: 现有依赖与目录已形成可复用基础，切换技术栈会扩大 MVP 周期；不引入 PG/Redis 或自建 SFU
  - 建议项: 图标只锁定 lucide-react@0.469.0
    理由: 仓库已使用 SVG 图标依赖，统一依赖满足 P0 规则并避免混用
  - 建议项: 将 session_id 贯穿 REST、TRTC、sidecar、bridge、Android 日志和指标
    理由: 当前 ready 帧虽生成 session_id，但无法证明全链路统一关联
  - 建议项: 语音期间降低 Windows 截图监控频率
    理由: README 已记录 12G 显存限制，监控与语音共享资源
  - 建议项: 先完成主链路门禁再灰度
    理由: 缺认证、背压、sidecar 和真机证据时，灰度会放大不可观测故障
  - 建议项: 隐私设置由本地 service 编排运行时动作与 SQLite 写入
    理由: 保证 UI 设置值与实际采集、上传、后台对话、桌面捕获行为一致，失败时可回滚
  - 建议项: 设备撤销采用强一致终止和最小审计
    理由: 仅标记数据库状态无法阻止已签发 userSig 或活动 session 继续使用
 evidence:
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\README.md:55-60
    line: 55-60
    说明: WGC/模型/12G 显存等已知平台约束；语音期间不得维持高频双模型负载
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\pet-ui\package.json:12-26
    line: 12-26
    说明: React 18.3.1、TypeScript 5.6.3、XState 5.19.0、lucide-react 0.469.0、Tauri API 2.11.1/CLI 2.11.4；当前使用 ^ 范围版本
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\pet-ui\src-tauri\Cargo.toml:7-12
    line: 7-12
    说明: Tauri 2.11.5 仅启用 tray-icon/image-png，未见 shell/autostart/capability/externalBin 完整闭环
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\build.gradle.kts:6-20,54-74
    line: 6-20,54-74
    说明: Android target 35/min 26、Java/Kotlin 17；TRTC 13.4.0.20477、OkHttp 4.12.0、Coroutines 1.8.1 精确声明
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\AndroidManifest.xml:4-21,46-54
    line: 4-21,46-54
    说明: RECORD_AUDIO、FOREGROUND_SERVICE_MICROPHONE、通知、悬浮窗和 microphone FGS 已声明；声明不等于后台真机能力
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\config.py:57-64
    line: 57-64
    说明: require_token=False、e2ee_enabled=False 是开发默认，生产必须 fail-closed
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\rtc_session.py:58-118
    line: 58-118
    说明: room_id 按 device_id 派生、userSig TTL 配置为 600s、SecretKey 从 Settings 注入；仍缺设备凭证绑定和限流
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\backend\app\api\routes_voice.py:39-50,125-177
    line: 39-50,125-177
    说明: `/session` 与 `/session/sign` 当前请求体只有 device_id/user_id，路由未呈现 Authorization、nonce 和限流
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\backend\app\voice\session.py:221-253,283-309
    line: 221-253,283-309
    说明: hello token 仅在 require_token 时校验，device_id 可由客户端自报；ready 帧生成 session_id
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\backend\rtc_bridge\session.py:55-110
    line: 55-110
    说明: PeerVoiceSession 上行 asyncio.Queue 无界，已有 frame/byte 统计但无背压指标
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\backend\rtc_bridge\shaper.py:16-66
    line: 16-66
    说明: 下行按 20ms/640B 拆帧并节拍推送，但 Queue 无界，尾帧策略未闭合
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\voice\VoiceForegroundService.kt:253-320
    line: 253-320
    说明: mic handoff、签发、进房和退房通过 Job/布尔量协调，存在并发顺序风险
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\voice\VoiceState.kt:4-13,21-32
    line: 4-13,21-32
    说明: 当前六态与 VoiceUiState 尚不包含 requesting_permission、endpointing、interrupted、recovering 等完整商业体验态
  - artifact_ref: C:\Users\Administrator\WorkBuddy\监视app\mobile-app\app\src\main\java\com\jax\voice\net\RtcClient.kt:281-344,392-436
    line: 281-344,392-436
    说明: enterRoom/startLocalAudio、exitRoom 异步回调和 daemon Thread 超时兜底；需要统一 dispatcher
  - artifact_ref: https://tauri.app/develop/sidecar/
    line: n/a
    说明: Tauri 官方要求 externalBin、目标架构后缀和 shell capability allow-execute
  - artifact_ref: https://developer.android.com/about/versions/14/changes/fgs-types-required
    line: n/a
    说明: Android 14 target 必须声明 FGS 类型/权限，microphone 需 RECORD_AUDIO 且后台启动受限制
```
