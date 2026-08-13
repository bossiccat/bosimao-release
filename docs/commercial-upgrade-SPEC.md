# Spec - 波斯猫双工语音商业升级 v1.1

> 生成日期：2026-08-07
> 基于：`commercial-upgrade-PRD.md` + `commercial-upgrade-ARCHITECTURE.md` + `commercial-upgrade-DESIGN.md`
> 状态：已确认并锁定
> 当前实现裁决：FAIL。本文是后续设计、开发、测试和发布的唯一规格契约，不代表代码或真机能力已通过。

---

## 0. 未决项复现与裁决

本阶段复现 `docs/decisions/OPEN-DECISIONS.md`：16 个历史条目中，与本次商业升级直接相关的未决项为 3 个，已决或被本 Spec 覆盖的条目为 13 个。

| ID | 当前状态 | 本 Spec 裁决 |
|---|---|---|
| O-001 全双工语音 MVP 归属 | RESOLVED | 全双工是 P0 唯一商业主体验，不再作为后续版本候选 |
| O-003 语音唤醒方式 | OPEN | 唤醒词为 P1 Beta；P0 只验收 Android 主页面手动入口、悬浮球和通知按钮，其中悬浮球与通知按钮必须可独立发起；唤醒词不进入 Phase 1.5 DoD |
| O-014 手机语音对话 | RESOLVED | 自研 Android + TRTC 是正式主路径；飞书语音不承担商业主体验 |
| O-015 本地模型原生全双工 | RESOLVED | MiniCPM-o/APM 原生流式双向与 barge-in 是 P0 唯一正式模型路径；自动半双工 fallback 不进入 Phase 1.5 DoD，仅保留为 P1 独立能力 |
| O-016 生产域名 | OPEN | 生产必须使用 TLS 和已备案稳定域名；外部域名与证书未就绪前不得生产放行 |
| TRTC Electron 注入签名与格式 | OPEN | 以实际 SDK 包、官方签名、兼容矩阵、干净安装和真机证据锁定，不接受历史 48 kHz 假设 |

未决项分类：O-003 为 `design-decision-to-evaluate`；O-016 为 `waiting-on-external-condition`；TRTC Electron 注入契约为 `existing-design-boundary`。实现不得绕过未决项宣称对应能力已完成。

## 1. 产品定义

- **一句话描述**：让单用户通过 Android 手机与 Windows 上的波斯猫助手进行可打断、可恢复、可审计的原生全双工语音对话，并查看用户授权的桌面 agent 状态。
- **目标用户**：同时使用 Codex、Trae、Hermes、WorkBuddy 的 Windows AI 编程重度用户。
- **核心问题**：现有原型只能证明局部连接，不能证明 Android 扬声器真实连续播放；会话竞态、安全缺口、无界音频队列和 sidecar 交付漂移阻断商业发布。
- **部署范围**：单用户、1 台 Windows 电脑、1 台 Android 手机。

## 2. MVP 范围

| 优先级 | 功能 | 验收摘要 | RICE |
|---|---|---|---:|
| P0 | 设备配对、凭证、列表、撤销 | Bearer credential、sidecar 独立 credential、nonce、防重放、限流、强一致撤销 | 5.40 |
| P0 | 串行会话生命周期 | `IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE`，取消、超时、退出、重进均可恢复 | 4.86 |
| P0 | 成熟 RTC 全双工主链路 | Android TRTC 上行、PC sidecar、bounded bridge、APM、TRTC 下行、Android 扬声器 | 4.32 |
| P0 | 固定音频帧和背压 | 模型侧 16 kHz、mono、PCM16、20 ms、640 bytes；跨块 residue；限帧、限字节、限帧龄 | 4.32 |
| P0 | 全链路可观测和诊断 | `session_id/turn_id`、首远端帧、首非零播放、队列、丢帧、重连、错误 | 4.20 |
| P0 | Android 真机连续两轮 | 两轮均有非零采集、上行、下行、远端首帧及扬声器播放证据 | 3.38 |
| P0 | Windows 常驻与 sidecar 服务化 | externalBin、最小 capability、自启、单实例、托盘、watchdog、退出清理 | 3.20 |
| P0 | 隐私和数据权利 | 转写默认不持久化；可选本地加密保存、删除、导出；脱敏诊断；四类开关即时生效 | 3.20 |
| P1 | 本地半双工兼容模式 | 独立入口、用户显式选择，不允许 P0 主链路失败后自动降级并伪装为全双工 | 2.40 |
| P1 | 唤醒词 Beta | 仅在真机功耗、误唤醒、后台限制门禁通过后默认开启 | 2.00 |

## 3. 明确不做

| 不做的功能 | 原因 | 何时考虑 |
|---|---|---|
| 多用户、团队、企业 SSO | 超出单用户 MVP，增加租户和授权复杂度 | v2.0 |
| iOS、视频、屏幕分享 | 不影响当前核心双工闭环 | Android 商业门禁通过后 |
| 自建 WebRTC SFU 或裸 WS 主媒体链路 | MVP 无法成熟承担 AEC、抖动、重连和自然打断 | 不计划 |
| 匿名生产 WS 或客户端自报特权身份 | 破坏认证和审计边界 | 不计划 |
| 云端保存原始音频、截图、代码 | 违反数据最小化和用户隐私基线 | 不计划 |
| 自动键鼠操控 agent | 需要独立安全评审和用户确认链路 | v2.0 |
| 云端历史语音、PG、Redis、向量库 | 单用户 SQLite 足够，避免过度设计 | 多用户版本 |
| 未经真机验证的唤醒词承诺 | 当前默认配置与能力证据不足 | P1 Beta 门禁后 |
| 宠物商店、社交、养成 | 与实时工作陪伴核心价值无关 | 商业主链路稳定后 |

## 4. 技术架构与版本锚定

| 层 | 技术 | 锁定版本 | 锁定原因 |
|---|---|---|---|
| Windows UI | React | 18.3.1 | 复用现有实现 |
| Windows 状态 | XState | 5.19.0 | 单一聚合状态模型 |
| Windows 宿主 | Tauri | Rust 2.11.5 / API 2.11.1 / CLI 2.11.4 | 常驻、小体积、sidecar 管理 |
| 图标 | Lucide | `lucide-react 0.469.0` | 全项目唯一 SVG 图标源 |
| Android | Kotlin / AGP | Kotlin 1.9.24 / AGP 8.6.1 / JVM 17 | 复用原生后台音频能力 |
| Android 平台 | Android SDK | compile 35 / target 35 / min 26 | 覆盖 Android 14+ FGS 规则 |
| Android RTC | LiteAVSDK_TRTC | 13.4.0.20477 | 现有精确依赖；最终需官方兼容矩阵和真机复核 |
| Android 网络 | OkHttp / Coroutines | 4.12.0 / 1.8.1 | 复用现有实现 |
| PC RTC sidecar | Node/Electron TRTC 适配器，由 Tauri externalBin 托管 | Electron 31.7.7 / SDK 13.4.802-beta.3 候选 | 唯一实现形态；Tauri/Rust 不实现 RTC 媒体，只负责受限启动、单实例、看门狗和退出清理；SDK 候选未放行 |
| 控制面 | Python / FastAPI | Python 3.11 生产基线；FastAPI 最终锁文件版本 | 复用现有服务和长连接 bridge |
| 本地数据 | SQLite | 运行时绑定版本 | 单用户最小数据集 |
| 模型 | MiniCPM-o/APM Realtime | 以部署实包锁定 | 原生流式双向和 barge-in |
| 媒体平面 | TRTC | 与 Android/Windows SDK 兼容矩阵一致 | 正式 RTC 主链路 |

所有 `package.json`、Python、Cargo、Gradle 依赖必须使用精确版本并提交 lockfile。禁止 `^`、`~`、`latest`、动态版本和未经验证的兼容假设。Python 生产依赖必须从干净隔离环境生成可重复锁定文件。

### 4.1 正式媒体拓扑

```text
Android microphone
  -> TRTC Android uplink
  -> PC TRTC sidecar
  -> bounded rtc_bridge
  -> MiniCPM-o/APM Realtime
  -> bounded DownlinkShaper
  -> PC TRTC sidecar
  -> TRTC Android downlink
  -> Android speaker
```

HTTP/WS 只用于签发、控制和 sidecar 本机桥接，不承担商业主媒体平面。P1 半双工兼容模式是用户显式选择的独立路径，禁止 P0 TRTC 主链路失败后自动切换并继续显示全双工状态。

### 4.3 Windows sidecar 唯一实现与文件边界

- `pet-ui/src-tauri/src/sidecar.rs`：Tauri/Rust supervisor，只负责 externalBin 存在性与哈希校验、固定参数启动、单实例、watchdog、优雅退出和超时终止；不得链接 TRTC SDK 或处理 PCM。
- `sidecar/main.js`：Node/Electron 进程入口和隐藏运行环境；负责 Electron 生命周期与崩溃退出，不持有业务会话状态。
- `sidecar/rtc.js`：唯一 Windows TRTC SDK adapter；负责进退房、远端音频帧事件和自定义音频注入。
- `sidecar/audio.js`：唯一音频格式 adapter；模型侧固定 16 kHz/mono/PCM16/20 ms/640 bytes，TRTC 注入采样率、帧长、字段和调用签名只能由实际 SDK 包、官方 Windows 契约和真机证据锁定。
- `sidecar/bridge.js`：仅连接 localhost `rtc_bridge`，传输固定协议控制帧和模型侧音频帧。
- `sidecar/config.js`、`sidecar/logger.js`：分别负责受限配置和脱敏日志；生产禁止本地 SecretKey fallback。

禁止另建第二套 Rust/native RTC adapter；也禁止把当前代码中的 48 kHz 注释、历史实验或候选实现写成正式协议事实。若候选 Node/Electron SDK 无法通过官方契约、干净安装和真机门禁，必须回到 Phase 1 架构变更流程，而不是实现者自行替换技术形态。

### 4.2 双层状态契约

会话生命周期仅允许：

```text
IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE
```

体验状态仅允许：

```text
idle
requesting_permission
connecting
listening
endpointing
thinking
speaking
interrupted
recovering
error
```

Android 和 Windows 只消费聚合 `VoiceUiModel`。禁止通过 `isListening`、`isThinking`、`isSpeaking`、`inCall`、`rtcExiting` 等并行业务布尔拼装 UI。所有命令在单一 coordinator/dispatcher 串行处理；旧 generation 迟到事件必须丢弃。

## 5. API 契约

统一响应：`{"code":0,"data":{},"message":""}`。受保护 HTTP 使用 `Authorization: Bearer <credential>`；其中 Android device credential 的 wire 格式锁定为 `<device_id>.<credential_secret>`，且请求体 `device_id` 必须与 Bearer 中的主体一致。有副作用请求和原子消费型控制面轮询额外使用 `X-Request-Nonce`。

配对引导由 Windows Tauri 本机 owner 发起：owner 调用 pairing-code 端点生成随机一次性 `pairing_code`，TTL 不超过 300 秒，只允许成功消费一次。Android 扫码或输入配对码后调用 register；此时 `pairing_code` 是 bootstrap 认证主体，register 不接受匿名注册，也不要求尚未存在的 device credential。成功响应必须包含仅展示一次的 `credential_secret`；服务端只保存其哈希。Android 普通存储只允许保存 `device_id`、IV 和密文，Vault 只加密保存 `credential_secret`，仅在签发请求取用时组合 wire token。

| Method | Path | 认证 | 请求 | 成功响应 |
|---|---|---|---|---|
| POST | `/api/v1/voice/devices/pairing-code` | 本机 owner credential + nonce | `{device_name_hint?,platform:"android"}` | `{pairing_code,expires_at,max_uses:1}` |
| POST | `/api/v1/voice/devices/register` | 一次性 pairing_code + nonce | `{pairing_code,device_name,platform,nonce}` | `{device_id,credential_id,credential_secret,expires_at}`，Secret 只展示一次 |
| GET | `/api/v1/voice/devices` | owner credential | 无 | `{items,total}` |
| POST | `/api/v1/voice/devices/{device_id}/revoke` | owner credential + nonce | `{reason}` | `{device_id,status,revoked_at,terminated_session_ids}` |
| POST | `/api/v1/voice/session` | device credential + nonce | `{device_id}` | `{session_id,room_id,user_id,sdk_app_id,user_sig,expires_at,scene}` |
| GET | `/api/v1/voice/session/pending` | sidecar credential + nonce | 无 | 原子领取零个或一个 `{session_id,device_id,room_id,expires_at}` 控制面意图；不得承载音频 |
| POST | `/api/v1/voice/session/sign` | sidecar credential + nonce | `{device_id,user_id}` | 同上，主体绑定 sidecar |
| GET | `/api/v1/voice/status` | device 或 sidecar credential | 无 | 聚合会话、帧、播放和队列指标 |
| WS | `/api/v1/voice/stream` | 会话绑定凭证 | hello 控制帧 | `ready/session_state/heartbeat/pong/transcript/error/reply_done` 与固定音频帧 |

控制指令只允许 `heartbeat/interrupt/pause/resume/close`。非法顺序返回 `40901 state_conflict`。架构细化阶段必须产出 `docs/api/commercial-voice-openapi.yaml`，OpenAPI 3.0 是前后端生成类型和实现的唯一 HTTP 契约。

错误码锁定：

| Code | Name | 语义 |
|---:|---|---|
| 40001 | `invalid_device_or_user` | 请求身份或字段非法 |
| 40101 | `auth_failed` | 凭证错误或缺失 |
| 40102 | `nonce_replay` | nonce 已消费或过期 |
| 40103 | `credential_revoked` | 凭证或会话已撤销 |
| 40401 | `device_not_found` | 设备不存在 |
| 40801 | `handshake_timeout` | 握手超时 |
| 40901 | `state_conflict` | 状态或指令顺序冲突 |
| 41301 | `queue_overflow` | 队列预算耗尽且无法恢复 |
| 42901 | `rate_limited` | device/IP 超限 |
| 50300 | `credential_unavailable` | 生产凭证或签名服务不可用 |
| 50401 | `upstream_timeout` | RTC 或模型上游超时 |

## 6. SQLite 数据模型

| 表 | 核心字段 | 索引与约束 |
|---|---|---|
| `settings` | `id,key,value_encrypted,created_at,updated_at` | unique(`key`) |
| `device_credentials` | `id,device_id,device_name,platform,credential_hash,status,expires_at,last_seen_at,revoked_at,revoke_reason,created_at,updated_at` | unique(`device_id`), index(`status`,`revoked_at`) |
| `pairing_codes` | `id,code_hash,created_by_owner_id,device_name_hint,platform,expires_at,consumed_at,consumed_device_id,created_at,updated_at` | unique(`code_hash`), index(`expires_at`,`consumed_at`)；只存哈希，最多消费一次 |
| `revoked_sessions` | `id,session_id,device_id,user_sig_fingerprint,expires_at,revoked_at,reason,created_at,updated_at` | unique(`session_id`), index(`device_id`,`expires_at`) |
| `session_events` | `id,session_id,turn_id,device_id,event_type,state,error_code,metadata_json,created_at,updated_at` | index(`session_id`,`device_id`,`created_at`) |
| `transcripts` | `id,session_id,ciphertext,encryption_version,started_at,ended_at,created_at,updated_at` | 仅保存显式开启后的 OS-bound key 密文；index(`session_id`,`created_at`) |
| `privacy_audit_events` | `id,action,subject_type,subject_id,result,metadata_redacted_json,created_at,updated_at` | index(`action`,`created_at`) |
| `consumed_nonces` | `id,subject_id,nonce_hash,expires_at,created_at,updated_at` | unique(`subject_id`,`nonce_hash`), TTL 清理 |
| `rate_limit_buckets` | `id,subject_id,route_key,window_start,count,created_at,updated_at` | unique(`subject_id`,`route_key`,`window_start`) |

Secret、设备 Secret 明文、TRTC SecretKey、userSig 明文、nonce 明文、原始音频、截图、代码内容和第三方 token 不得落库。删除转写不得在审计或诊断中保留正文副本。

## 7. 页面与交互清单

| 端 | 页面或入口 | 核心组件 | 对应契约 |
|---|---|---|---|
| Android | 主会话页 | 连接状态、转写、回复、固定 56px 主控、错误恢复 | session/status/VoiceUiModel |
| Android | 悬浮球 | 轻触发起、RMS 波形、状态图标 | VoiceUiModel |
| Android | 前台通知 | 暂停监听、立即对话、退出 | 会话命令 |
| Android | 权限引导 | 麦克风、通知、后台、电池优化、去设置、转文字 | privacy settings |
| Android | 设备与诊断设置 | 设备信息、隐私开关、诊断导出 | devices/local privacy service |
| Windows | 桌面宠物 | 简短状态、可访问锚点、提醒级别 | VoiceUiModel/monitoring |
| Windows | 紧凑会话面板 | listening/endpointing/thinking/speaking/error | VoiceUiModel/status |
| Windows | 设置页 | 设备列表和撤销、四类开关、转写保存/删除/导出、诊断导出 | devices/local services |
| Windows | 运行诊断 | SDK、模型、网络、麦克风、播放、sidecar 状态 | status/health |

所有可操作目标至少 44 x 44px。状态不能只依赖颜色；键盘、屏幕阅读器、`focus-visible` 和 reduced-motion 必须可用。主屏直接呈现产品操作，不制作营销 Hero。

## 8. Design Token 与图标契约

设计师细化阶段必须产出：

- `pet-ui/src/styles/design-tokens.json`
- `pet-ui/src/styles/design-tokens.css`
- Android 对应 `colors.xml`、`dimens.xml`、`themes.xml` 和 Lucide 同语义 VectorDrawable 映射

Token 定义值锁定为冷调深色中性界面，不使用紫粉渐变：

| Token | 值 | 用途 |
|---|---|---|
| `color.bg` | `#0B1118` | 应用背景 |
| `color.surface` | `#121B24` | 面板表面 |
| `color.surfaceRaised` | `#182530` | 抬升控件 |
| `color.text` | `#F3F7FA` | 主文本 |
| `color.textMuted` | `#9EB0BE` | 次文本 |
| `color.accent` | `#2BA8E0` | 主操作与焦点 |
| `color.success` | `#2FBF8F` | 成功 |
| `color.warning` | `#E3A93B` | 警告 |
| `color.danger` | `#E05A62` | 错误与撤销 |
| `color.border` | `#2A3B48` | 边界 |

字体栈：正文 Noto Sans SC；数值和状态码 JetBrains Mono；展示字体 MiSans 仅在授权确认后使用。图标系统全项目只锁定 Lucide，Web 使用 `lucide-react 0.469.0`，Android 使用同语义 VectorDrawable。尺寸统一为 16px 行内、20px 按钮内、24px 独立图标。禁止 emoji 功能图标和多图标库混用。

组件和业务样式不得出现 hex/rgb/rgba 字面量；颜色只允许在 Token 定义文件出现。禁止紫色到粉色渐变、发光边框叠加毛玻璃、空洞占位文案、弹跳/弹性缓动和虚假 Hero。

## 9. 安全与隐私

### 9.1 生产 fail-closed

生产环境缺少以下任一项时，服务必须拒绝启动或拒绝全部会话：设备凭证验证器、sidecar credential、nonce 存储、防重放、device/IP 限流、TLS 端点、TRTC SDKAppID/SecretKey。不得自动退回匿名 WS 或明文 relay。

- userSig TTL 不超过 600 秒，并绑定 `session_id/device_id/user_id/room_id`。
- 凭证 Secret 只展示一次，服务端只保存抗离线攻击的哈希。
- 撤销后 credential 立即失效，未过期 userSig 指纹进入撤销表，活动 session 终止并记录审计。
- nonce 只消费一次，存储哈希并按短 TTL 清理。
- 日志只允许脱敏标识、状态、错误、时延、帧数和队列指标。

### 9.2 本地隐私服务

| 接口 | 立即生效语义 |
|---|---|
| `setCloudProcessingEnabled(false)` | interrupt、flush、退出云端会话，拒绝新云端会话 |
| `setMicrophoneEnabled(false)` | 停止采集、清空上行队列、释放麦克风 owner |
| `setBackgroundConversationEnabled(false)` | 后台或锁屏时 pause、flush、退出房间 |
| `setDesktopCaptureEnabled(false)` | 停止 WGC 调度并释放帧，不改变语音生命周期 |
| `setPersistenceEnabled(false)` | 停止后续转写写入，既有数据单独确认删除 |
| `delete(transcript_id?)` | 删除单条或全部密文并记录不含正文的审计 |
| `export(format,destination)` | 仅本地解密后导出，不上传第三方 |
| `exportRedacted(destination)` | 仅导出脱敏诊断，用户确认范围和路径 |

setter 返回 `{applied_at,effective_value,action_result}`。SQLite 设置写入与运行时动作由同一 service 编排；运行时动作失败时回滚设置值。

## 10. EARS 验收标准

| ID | 功能 | 验收标准 | 优先级 |
|---|---|---|---|
| AC-01 | 配对码生成 | WHEN Windows 本机 owner 提交有效凭证和未使用 nonce，系统必须生成 TTL 不超过 300 秒且最多消费一次的 pairing_code，并只保存其哈希 | P0 |
| AC-02 | 设备注册 | WHEN Android 提交有效 pairing_code 和未使用 nonce，系统必须原子消费配对码、只绑定该设备并只展示一次 credential_secret；过期或已消费配对码必须拒绝 | P0 |
| AC-03 | 防重放 | IF nonce 已消费、过期或主体不匹配，系统必须返回 `40102` 并写脱敏审计 | P0 |
| AC-04 | 生产关闭失败 | IF 生产认证、TLS、限流或 TRTC 配置缺失，系统必须拒绝启动或拒绝全部会话 | P0 |
| AC-05 | 会话取消 | WHILE 处于 SIGNING，WHEN 用户取消，系统必须取消本地请求语义并直接回到 IDLE，不等待退房回调 | P0 |
| AC-06 | 进房取消 | WHILE 处于 ENTERING，WHEN 用户取消或超时，系统必须幂等退出并在有限时间回到 IDLE | P0 |
| AC-07 | 非法转换 | IF 会话命令与当前状态冲突，系统必须返回或记录 `40901`，不得静默执行 | P0 |
| AC-08 | 固定音频 | WHEN bridge 接收任意分块 PCM，系统必须跨块保留 residue，只向模型发送完整 640-byte 帧 | P0 |
| AC-09 | 尾帧 | WHEN 会话结束仍有不足帧 residue，系统必须按配置补零或丢弃并记录指标，不得发送变长帧 | P0 |
| AC-10 | 背压 | IF 队列超过 max_frames、max_bytes 或帧龄预算，系统必须丢旧保新或终止并记录 drops/backpressure | P0 |
| AC-11 | 真实播放 | WHEN 用户完成一轮对话，Android 必须记录远端首帧、非零播放时间和扬声器路由证据 | P0 |
| AC-12 | 连续两轮 | WHEN 同一 session 完成两轮，Android 两轮均必须可听，正常远端停止不得修改播放订阅 | P0 |
| AC-13 | 打断 | WHILE speaking，WHEN 用户开口或点击，Android 必须进入 interrupted 并在 P95 300 ms 内停止播放，再回 listening | P0 |
| AC-14 | 迟到音频 | IF 旧 generation 下行帧在打断后到达，系统必须丢弃，禁止重新播放 | P0 |
| AC-15 | 设备撤销 | WHEN owner 撤销设备，系统必须立即拒绝凭证和 userSig、终止活动 session 并回到 IDLE | P0 |
| AC-16 | 隐私默认 | WHILE 用户未开启转写持久化，系统不得创建 transcripts 正文记录 | P0 |
| AC-17 | 隐私开关 | WHEN 用户关闭云端、麦克风、后台对话或桌面捕获，系统必须立即停止对应动作；失败必须回滚 UI 设置 | P0 |
| AC-18 | 诊断导出 | WHEN 用户导出诊断，文件必须不含凭证、nonce、原始音频、截图、代码、完整文本或敏感路径 | P0 |
| AC-19 | sidecar | WHEN Windows 应用启动，系统必须校验 sidecar 存在和哈希、单实例启动并由 watchdog 监控 | P0 |
| AC-20 | 故障恢复 | WHEN RTC、模型、网络、麦克风或 sidecar 故障，UI 必须在 2 秒内显示分类原因和可执行恢复动作 | P0 |
| AC-21 | 可访问性 | WHEN 使用键盘、屏幕阅读器或 reduced-motion，所有核心操作和状态必须保持可理解、可执行 | P1 |

## 11. 边界、性能与已知陷阱

### 11.1 性能预算

- 连接成功提示：不超过 10 秒。
- 语音首字：P50 不超过 1.5 秒。
- 用户开口或点击到 Android 停止播放：P95 不超过 300 ms。
- 核心错误分类反馈：不超过 2 秒。
- 模型侧音频：16 kHz、mono、PCM16、20 ms、640 bytes。
- 队列必须同时限制 `max_frames`、`max_bytes` 和最大帧龄；具体容量由压力测试锁定并记录在运行配置。

### 11.2 平台边界

- Windows 目标为 `x86_64-pc-windows-msvc`，新增架构必须进入独立构建矩阵。
- Android minSdk 26、targetSdk 35；Android 14+ microphone FGS、后台启动和锁屏能力必须真机验收。
- 语音期间 Windows 监控降低采样频率，避免 12 GB 显存环境下双模型争用。
- 唤醒词在真机误唤醒、功耗和后台限制通过前保持 P1 Beta。

### 11.3 内嵌已知坑

| 签名 | 技术栈指纹 | 根因 | 强制修法 |
|---|---|---|---|
| `sdk/trtc-13.4-audio-frame-listener-api-renamed` | LiteAVSDK_TRTC 13.4.0.20477 | 旧文档 API 名称、参数和 data 类型已变化 | 实现前用实际 AAR、`javap` 或类常量池核对签名，禁止按记忆编写 |
| `protocol/heartbeat/relay-client-replies-pong-to-voice-gateway` | gateway/relay 双协议 | 网关 ping 需要 heartbeat，中继 ping 才回复 pong | 分离协议处理并用契约测试锁定 |
| `runtime/proxy-127.0.0.1-7890/network-failure` | Windows 构建环境 | 失效代理导致依赖下载失败 | 干净构建先验证代理可达性，不得把网络失败误判为源码失败 |
| `build/powershell-5.1-no-bom-gbk-garbled` | WinPS 5.1 | 无 BOM 脚本按 GBK 解析 | 不新增含非 ASCII 路径的脚本；现有必要脚本使用 UTF-8 BOM |
| Android Gradle native lock | Gradle 8.7 / Windows | `native-platform.dll.lock` 权限阻断初始化 | 隔离 Gradle user home 后重试，并单独标记环境失败与源码失败 |
| sidecar SDK partial directory | npm / TRTC Electron | 包目录处于隐藏临时状态，正式路径缺失 | 干净安装、`npm ls`、SDK 版本日志、哈希和冒烟同时通过 |

## 12. 端到端验证与发布门禁

### 12.1 自动化验证

```bash
# Python 单测与集成测试
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests -q

# Windows UI
C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2/node.exe --version
npm --prefix pet-ui run build

# sidecar 依赖和冒烟
npm --prefix sidecar ls --depth=0
npm --prefix sidecar run sidecar

# Android JVM 测试与构建
mobile-app/gradlew.bat -p mobile-app testDebugUnitTest
mobile-app/gradlew.bat -p mobile-app assembleDebug
```

以上命令必须在干净 checkout 和提交锁文件的环境中复跑。命令成功只证明局部构建，不等于真实跨端音频通过。

### 12.2 真机验证

至少使用 1 台 Android 真机，连续执行两轮：

1. 以悬浮球发起会话，记录 `session_id`。
2. 第一轮采集非零 RMS，验证 sidecar 上行帧和字节增长。
3. 验证 bridge/APM 下行帧和字节增长。
4. Android 记录远端首帧、首个非零播放、音频焦点、扬声器路由和用户可听确认。
5. 第二轮重复步骤 2 至 4，证明播放订阅未被正常远端停止事件静音。
6. speaking 中开口和点击各执行一次打断，采集 Android 停止播放 P95。
7. 执行暂停/恢复、退出/重进、断网恢复、锁屏/后台和 sidecar 崩溃恢复。
8. 撤销当前设备，验证活动 session 终止，旧 credential/userSig/WS 全部被拒绝。

### 12.3 发布阻断项

以下全部归零前，商业发布裁决保持 FAIL：

- sidecar TRTC SDK 干净安装、运行时版本、哈希和冒烟未通过。
- 生产认证、nonce、防重放、限流、TLS 或 fail-closed 任一缺失。
- 上下行任一无界队列、变长尾帧或无帧龄预算。
- Android 会话仍依赖并行业务布尔或旁路线程重入。
- 正常远端停止仍会触发播放订阅静音。
- Tauri externalBin、最小 capability、自启、单实例、托盘、watchdog 任一缺失。
- 依赖仍包含范围版本或缺失 lockfile。
- Android 真机连续两轮、非零播放和 300 ms 打断证据缺失。
- P0 静态扫描发现 emoji 功能图标、紫粉渐变、Token 外硬编码颜色、空洞文案或虚假 Hero。

## 13. 变更记录

| 日期 | 版本 | 变更内容 | 原因 | 影响范围 |
|---|---|---|---|---|
| 2026-08-07 | 1.0 | 基于用户确认的 PRD、架构、设计基线生成单一商业升级契约 | Phase 1.5 锁定后续开发范围和门禁 | 后端、bridge、Android、sidecar、Tauri、UI、QA、交付 |
| 2026-08-07 | 1.1 | 冻结 P0 手动/通知发起与全双工主链路；半双工和唤醒词留 P1；锁定 Node/Electron sidecar 文件边界；补齐 pairing_code 生成、TTL、单次消费和一次性 Secret | 架构师 Phase 1.5 复核发现三项单向裁决缺口 | Spec、OpenAPI、SQLite、Task 1/2/4/9/10 |
| 2026-08-08 | 1.2 | 锁定 Android device Bearer 为 `<device_id>.<credential_secret>`，Vault 仅加密保存 Secret，取用时组合 | Android 裸 Secret 与后端主体解析不一致会导致配对后 session 签发 401 | Spec、OpenAPI、Android 配对与会话签发、契约测试 |
