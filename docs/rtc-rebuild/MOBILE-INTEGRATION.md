# 手机端 RTC 集成方案（Android）— MOBILE-INTEGRATION

> 状态：**已按架构定稿**（TRTC 最终选型；Agora 否决，仅留选型记录）· 2026-08-05 fe-mobile
> 依据：docs/rtc-rebuild/ARCHITECTURE.md（§3 架构 / §4 删留清单 / §5.1 手机端要点）
> 范围：手机端（mobile-app/，Kotlin + OkHttp，AGP 8.6.1，minSdk 26 / compileSdk 35 / targetSdk 35）
> 背景：推倒自研 WS 中继链路（VoiceWsClient 配对状态机 / 重连风暴是 bug 重灾区），替换为腾讯 TRTC 纯音频通话。
> 约束：UI / 唤醒词 KWS / AudioRecord 采集 / AudioTrack 播放 的**非连接层**代码保留不动；本文档只给出连接层（VoiceWsClient → RtcClient）的替换设计与 TRTC SDK 集成方式。

---

## 0. TL;DR（30 秒结论）

- **选型（架构已定）**：腾讯 TRTC，`TRTCAppSceneAudioCall` 纯语音场景；不用声网 Agora（否决理由见 §5）。
- **连接层替换**：`VoiceWsClient.kt`（WS + 中继配对 + AudioTrack 播放）→ 新建 `RtcClient.kt`（TRTC 进房/退房/采集/播放/状态回调）。
- **会话流程（仅会话期进房，常驻监听不耗 RTC 分钟）**：本地 KWS 唤醒 → REST `POST <云函数>/api/v1/voice/session` 向**云函数**拉取 roomId + userSig（云函数代签，见 §2.2）→ `enterRoom` → 对话（全双工）→ 静默超时/结束 → `exitRoom`。
- **关键设计：麦克风独占交接（mic handoff）**。Android 同一 App 不能两个 AudioRecord 同时采集（TRTC SDK 采集与本地 KWS AudioRecord 互斥）。`MicRecorder` 保留做**监听阶段**的 KWS 常驻采集；唤醒命中 → **停 MicRecorder 释放 mic** → `RtcClient.enterRoom()` → TRTC SDK 自行采集上行 → 通话结束 `exitRoom()` → **重启 MicRecorder** 恢复"一直在听"。
- **播放（决策点，MVP 建议）**：优先 TRTC SDK 自动播放（自动订阅模式，最简单）；若需 VAD 打断/波形显示，注册音频回调接管远端 PCM 走现有 AudioTrack 播放器（保留 playGen 打断机制）。**MVP 先 SDK 自动播放**，打断用 `onRemoteUserAudioStatus` + 本地 VAD 做状态机驱动。
- **删除**：VoiceWsClient 的 WS/配对/心跳/重连逻辑、FrameCodec、PairFrame、VoiceCipher（应用层 E2EE 无法作用于编码后媒体流，RTC 传输层加密替代）。
- **保留**：MainActivity / SettingsActivity / VoiceController 状态总线 / VoiceForegroundService 前台服务骨架 / MicRecorder / WakeWordEngine / VadEngine（预留）/ AudioTrack（可选播放路径）/ 悬浮窗。

---

## 1. TRTC Android SDK 集成（最终选型）

### 1.1 依赖与工程配置

```kotlin
// settings.gradle.kts —— 已确认 mavenCentral() 在 pluginManagement 与 dependencyResolutionManagement 均已配置，无需改动

// app/build.gradle.kts
dependencies {
    // TRTC 精简版 SDK（仅 TRTC 通话 + 直播播放）；锁精确版本，禁止 latest.release
    implementation("com.tencent.liteav:LiteAVSDK_TRTC:13.4") // 以 mavenCentral 实际精确版本为准（13.4.x，2026-06 发布）；写死版本号防升级破坏（ADR 要求）
}

android {
    defaultConfig {
        // TRTC 官方要求指定 CPU 架构（缩包体）
        ndk {
            abiFilters += listOf("armeabi-v7a", "arm64-v8a")
        }
    }
}
```

```proguard
# proguard-rules.pro
-keep class com.tencent.** { *; }
```

- **minSdk**：TRTC 官方最低 Android 4.4（API 19）；项目 minSdk 26 满足。
- **targetSdk 35 / Android 12+ 蓝牙**：如需蓝牙耳机支持，运行时动态申请 `BLUETOOTH_CONNECT`（普通权限级）；MVP 可先不加。

### 1.2 权限（AndroidManifest.xml）

现有 `RECORD_AUDIO` 已满足核心采集；TRTC 官方清单补齐（均为普通权限，运行时逻辑不变）：

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
<uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />
<uses-permission android:name="android.permission.RECORD_AUDIO" />          <!-- 已有 -->
<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />
<uses-permission android:name="android.permission.BLUETOOTH" />
<!-- 纯音频不需要 CAMERA；勿设 android:hardwareAccelerated="false"（默认即可） -->
```

### 1.3 纯音频通话关键 API（Android，Kotlin 视角）

```kotlin
// 创建实例（App 进程级单例）
val cloud = TRTCCloud.sharedInstance(appContext)
cloud.addListener(listener)

// 进房参数（roomId/userSig 由云函数签发，见 §2.2）
val params = TRTCCloudDef.TRTCParams().apply {
    sdkAppId = SDK_APP_ID            // 控制台创建 TRTC 应用得到（与 CloudBase 同账号）
    userId = deviceId                // 手机端用现有 deviceId（如 "jax-xxxxxxxx"）
    userSig = session.userSig        // 云函数代签（SecretKey 唯一存云函数环境变量，禁止硬编码进 APK）
    strRoomId = session.roomId       // 字符串房间号（≤64 字节）；用 strRoomId 时 intRoomId 必须为 0
}

// 进房（纯音频场景）+ 开本地采集上行（不调用 startLocalPreview = 纯音频）
cloud.enterRoom(params, TRTCCloudDef.TRTC_APP_SCENE_AUDIOCALL) // TRTCAppSceneAudioCall
cloud.startLocalAudio(TRTCCloudDef.TRTC_AUDIO_QUALITY_SPEECH)  // 语音档（16k），与现有 16k 采集链路一致，减少重采样

// 退房
cloud.exitRoom()   // 回调 onExitRoom(reason)，reason: 0主动退出 / 1被踢 / 2房间解散
```

**音量回调（替换原 RMS 计算，驱动悬浮窗波形）**：

```kotlin
cloud.enableAudioVolumeEvaluation(300, false) // 300ms 间隔；第二参 enableVad
// 回调 onUserVoiceVolume(userVolumes: ArrayList<TRTCVolumeInfo>, totalVolume: Int)
//   userVolumes 中 userId 为空串 = 本地音量（0~100）
```

**音频路由 / 音量 / 静音**：

```kotlin
cloud.setAudioRoute(TRTCCloudDef.TRTC_AUDIO_ROUTE_SPEAKER) // 扬声器外放；EARPIECE=听筒
cloud.setSystemVolumeType(TRTCCloudDef.TRTCSystemVolumeTypeAuto) // VOIP/Auto/Media
cloud.setAudioCaptureVolume(100)   // 本地采集音量
cloud.setRemoteAudioVolume(userId, 100) // 单远端播放音量
cloud.muteLocalAudio(true/false)   // 静音（继续发静音包）；stopLocalAudio=完全停采集上行
```

### 1.4 TRTC 自动重连（SDK 内置，应用层只驱动六态 UI）

TRTC 支持**无限重连**（断网自动重进房），时序回调：

| 回调 | 时机 | UI 映射 |
|---|---|---|
| `onConnectionLost` | 断连（约连续 8s 未连上服务端） | CONNECTING + "网络中断，重连中…" |
| `onTryToReconnect` | 断连 3s 后开始尝试，之后每 24s 重试 | CONNECTING |
| `onConnectionRecovery` | 任意时刻重连成功 | CONNECTED（清错误） |

其他关键回调：`onEnterRoom(result)`（result>0 成功=耗时ms，<0 失败=错误码）、`onRemoteUserEnterRoom(userId)`、`onRemoteUserLeaveRoom(userId, reason)`、`onFirstAudioFrame(userId)`（远端首帧音频=可播）、`onRemoteUserAudioStatus(userId, audioRecvState, reason)`（远端是否在说话，本地 VAD 打断/状态机用）、`onAudioRouteChanged`、`onMicDidReady`、`onError(errCode, errMsg, extra)`。

### 1.5 加密（E2EE）

- **默认（推荐）**：TRTC 内置 **DTLS-SRTP 传输加密** + 房间鉴权（UserSig）+ TLS 控制面。对本项目"录音不出必要范围"（手机↔PC 实时传输、不落盘不进日志）足够；SecretKey 唯一存云函数环境变量，userSig 短时效。
- **媒体流私有加密（可选，付费）**：`enablePayloadPrivateEncryption(enabled, TRTCPayloadPrivateEncryptionConfig)`，进房前调用，退出自动关闭，房间内所有端同配置：
  - `TRTCEncryptionAlgorithmAes128Gcm`：key 16 字节；`TRTCEncryptionAlgorithmAes256Gcm`：key 32 字节；`encryptionSalt` 32 字节（不可全 0）。
  - **⚠️ 需购买 RTC-Engine 专业版/旗舰版 + 控制台业务审核**（个人项目默认不可用）→ 若非硬需求不启用，用默认传输加密即可。
- **删除 `VoiceCipher.kt`**（架构 §4.1 确认）：应用层 AES-GCM 帧加密只能作用于自定义帧（FrameCodec），RTC 链路走 Opus 编码流，应用层加密无法作用，由 SDK 传输层加密替代。

---

## 2. RtcClient 设计（替换 VoiceWsClient）

> 新文件：`mobile-app/app/src/main/java/com/jax/voice/net/RtcClient.kt`（与 VoiceWsClient 同包，业务侧改动最小）

### 2.1 类职责与对外接口（伪码级，实现阶段填充）

```kotlin
/** TRTC 通话客户端 —— 纯音频 1v1 通话；替代 VoiceWsClient（WS+配对） */
class RtcClient(
    private val appContext: Context,
    private val onState: (ConnectionState) -> Unit,   // 复用现有枚举 DISCONNECTED/CONNECTING/CONNECTED
    private val onPhase: (VoicePhase) -> Unit,        // 复用：LISTENING/THINKING/SPEAKING 等
    private val onRms: (Float) -> Unit,               // 复用：通话中音量回调 → 悬浮窗波形
    private val onError: (code: String, msg: String) -> Unit
) {
    // ---- 生命周期 ----
    fun init()                                  // TRTCCloud.sharedInstance + addListener（App 进程级一次）
    fun enterRoom(roomId: String, userSig: String) // 进房（AudioCall）+ startLocalAudio(SPEECH)
    fun exitRoom()                              // 退房 + 停采集（释放 mic，供 KWS 恢复）
    fun release()                               // 销毁引擎（服务停止时）
    fun muteLocal(muted: Boolean)               // 静音/恢复上行
    fun isInRoom(): Boolean

    // ---- 内部映射 ----
    private fun onEnterRoomSuccess()   → onState(CONNECTED) + 清 lastError
    private fun onEnterRoomFail(code)  → onState(DISCONNECTED) + onError(code, msg)
    private fun onRemoteUserJoin()     → onState(CONNECTED)（远端有流=可通话）
    private fun onRemoteUserLeave()    → 提示"对端已退出" + 保持房间等待重连（或自动退房，按产品定）
    private fun onConnLost()           → onState(CONNECTING)（SDK 自动重连，UI 显示"重连中"）
    private fun onConnRecovery()       → onState(CONNECTED)
}
```

### 2.2 服务端契约（手机 ↔ PC，schema 见 ARCHITECTURE.md §5.2 / be-pc）

| 项 | 值 |
|---|---|
| 会话签发接口 | `POST <云函数>/api/v1/voice/session`（**云函数代签**，方案 A 架构裁决 ARCHITECTURE §3.4；SecretKey 唯一存云函数环境变量，手机/PC 均不持有） |
| 请求 | 手机携带 `device_id`（pairing_code 语义废弃可省） |
| 响应 | `{ room_id: String, user_id: String, user_sig: String, sdk_app_id: Int, scene: "audio_call" }`（**wire 层 snake_case**；Kotlin data class 映射为 roomId/userId/userSig/sdkAppId） |
| 用户 ID | 手机 = `device_id`（现有 `VoiceConfig.deviceId()`）；PC sidecar = `jax-pc-sidecar`（be-pc 定） |
| 房间号 | **规则定稿**：`room_id = TRTC_ROOM_PREFIX + device_id`（`TRTC_ROOM_PREFIX` = `jax-`，如 `jax-<device_id>`）；同 device 幂等复用房间 |
| 加密 | 默认 DTLS-SRTP；私有加密为付费可选（§1.5） |
| 音频场景 | `TRTCAppSceneAudioCall` + `TRTC_AUDIO_QUALITY_SPEECH`（16k 语音档） |
| 采样率 | 手机端不感知（RTC 内部 Opus）；PC sidecar 回调 48k f32 → 16k s16 由 be-pc 负责 |

> ⚠️ **userSig 严禁硬编码**：TRTC 控制台 SecretKey 唯一存**云函数环境变量**（架构 §3.4 / R8）；手机端每次唤醒经 REST 拉取短时效 userSig，App 内不持有任何密钥。

### 2.3 状态映射（复用现有 VoiceController / VoiceState，UI 零改动）

| RTC 事件 | ConnectionState | VoicePhase | lastError |
|---|---|---|---|
| enterRoom 发起 | CONNECTING | — | 清空 |
| onEnterRoom 成功 | CONNECTED | LISTENING | — |
| 远端进房/首帧音频 | CONNECTED | LISTENING | — |
| onConnectionLost | CONNECTING | — | "网络中断，重连中…" |
| onTryToReconnect | CONNECTING | — | 同上 |
| onConnectionRecovery | CONNECTED | — | 清空 |
| 进房失败 onError | DISCONNECTED | MONITORING | "进房失败: <code> <msg>" |
| exitRoom（主动） | DISCONNECTED | MONITORING | — |

- 六态（listening/thinking/speaking）驱动：`session_state` 经现有 EventBus/推送下行（架构 §3.3 控制面，不发明新实时控制协议）；本地 `onRemoteUserAudioStatus` + VAD 兜底。

---

## 3. 与现有模块衔接（mic handoff 是核心）

### 3.1 当前管线（改造前）

```
MicRecorder(AudioRecord 16k) ──► FrameDispatcher ──┬─► WakeWordEngine(KWS)
                                                    ├─► RMS → VoiceController
                                                    └─► uplink → VoiceWsClient.sendAudio → WS → PC
VoiceWsClient 下行 ──► AudioTrack 播放
```

### 3.2 改造后管线

```
【监听阶段】(一直) MicRecorder ──► FrameDispatcher ──┬─► WakeWordEngine(KWS)   （RMS 保留）
                                                    └─► RMS → VoiceController
【唤醒命中】→ 停 MicRecorder（释放 mic）→ REST POST /api/v1/voice/session 拉 roomId+userSig
【通话阶段】RtcClient.enterRoom → TRTC SDK 采集上行 + 播放下行（SDK 内置 AEC/NS/AGC）
            音量回调 onUserVoiceVolume → VoiceController.setRms
            ⚠️ mic 已被 TRTC SDK 独占：本地 VAD/barge-in 不能再用 MicRecorder（已 stop）
            → 打断判定改用 TRTC 播放状态 + 远端音频状态（见 §3.4）
【通话结束】(VAD 静默 + 回复结束 15s 超时) RtcClient.exitRoom（释放 mic）→ 重启 MicRecorder（恢复"一直在听"）
```

### 3.3 逐模块衔接结论

| 模块 | 处置 | 说明 |
|---|---|---|
| `VoiceForegroundService` | **改** | 骨架保留；`triggerWake` 内改为「停 MicRecorder → 拉会话 → RtcClient.enterRoom」；`endUplink`/静默超时改为「RtcClient.exitRoom → 重启 MicRecorder」；移除 `connectGateway`（LAN/RELAY 选择逻辑删除） |
| `MicRecorder` | **保留** | 仅监听阶段运行；通话阶段必须 stop（与 TRTC mic 互斥）；其 `onUplink` 分支废弃 |
| `WakeWordEngine` | **保留** | 不变；KWS 仍需 16k PCM（由 MicRecorder 提供） |
| `VadEngine` | **保留/预留** | 架构 §4.2 列为保留资产；当前代码仅 FrameDispatcher 有 VAD 注释占位（M2 预留），无独立 VadEngine.kt。**注意：仅监听阶段可用 MicRecorder 的 PCM 做 VAD；通话阶段 mic 被 TRTC 独占，VAD/barge-in 改走 SDK 回调（§3.4）** |
| `FrameDispatcher` | **保留（改）** | 删除 `onUplink`/`setUplink` 分支；保留 KWS 分发 + RMS + VAD 钩子 |
| `AudioTrack` 播放 | **保留（可选路径）** | **MVP 先用 TRTC SDK 自动播放**（自动订阅模式，远端音频自动解码播放），AudioTrack 不参与；若需 VAD 打断/波形显示，注册 `onAudioFrame` 回调接管远端 PCM 走现有 AudioTrack 播放器（保留 playGen 打断机制）。架构 §5.1 决策点，MVP 选 SDK 自动播放 |
| `VoiceWsClient` | **删除** | WS 连接/配对状态机/心跳/指数退避重连/playExecutor 全删（TRTC SDK 内置重连，无重连风暴） |
| `FrameCodec` / `PairFrame` | **删除** | RTC 走 Opus 编解码，无需自定义帧头 seq/ts；配对语义废弃 |
| `VoiceCipher` | **删除** | 架构 §4.1 确认：应用层 E2EE 无法作用于编码后媒体流；RTC 传输层加密替代 |
| `VoiceController` / `VoiceState` | **保留** | 状态总线不变；`ConnectionState` 三态语义沿用；`lastError` 展示 RTC 错误码 |
| `MainActivity` / `SettingsActivity` / `FloatingOverlay` / `WaveformView` | **保留** | UI 零改动（RMS 数据源从 AudioRecord 换为 RTC 音量回调） |
| `VoiceConfig` | **改** | 删除 LAN/RELAY 连接配置入口（或标记废弃）；新增 TRTC 配置：SDKAppID、会话接口地址；`pairingCode`/`deviceId` 保留（会话签发入参） |

### 3.4 通话期打断（barge-in）——mic handoff 的直接推论

> 架构确认（ARCHITECTURE.md §5.1 已标注）：会话期间 mic 被 TRTC SDK 独占，**本地 VAD 不能依赖 MicRecorder**（已 stop）。打断判定来源从「本地 mic VAD」变为「SDK 播放状态 + 远端音频状态」。

**实现路径（MVP，不新增协议）**：

| 场景 | 判定来源 | 动作 |
|---|---|---|
| 对端回复中用户开口打断 | 手机持续上行（TRTC SDK 采集）→ 对端 MiniCPM-o 全双工原生 barge-in | 手机侧无需显式判定；可选本地静音/停播兜底 |
| 对端停止说话（回复结束） | TRTC `onRemoteUserAudioStatus(userId, audioRecvState, reason)` 或播放状态回调 | 六态 Speaking → Listening |
| 本地打断停播兜底 | `onRemoteUserAudioStatus` + 播放状态机 | 停播下行（SDK 自动播放时走 `muteLocalAudio`/退出逻辑，不依赖 playGen） |

- **波形/打断若需更细控制**：注册 `onAudioFrame` 回调接管远端 PCM 走现有 AudioTrack + playGen（§3.3 可选路径，MVP 不做）。
- **对 QA 的口径影响**：验收「Speaking 中说停 <500ms」的实现路径变了（来源：SDK 播放/远端音频状态，而非本地 mic VAD），已与 qa 对齐（见 §6）。

---

## 4. 改造范围清单（对齐架构 §4）

### 4.1 删除（连接层）

- [ ] `net/VoiceWsClient.kt`（WS 客户端 + 配对状态机 + AudioTrack + 心跳 + 指数退避重连）
- [ ] `net/FrameCodec.kt`（自定义音频帧编解码）
- [ ] `net/PairFrame.kt`（中继配对帧）
- [ ] `crypto/VoiceCipher.kt`（应用层 E2EE，RTC 传输层加密替代）
- [ ] `test/.../FrameCodecTest.kt`、`VoiceCipherTest.kt`（随实现删除，架构 §4.1）
- [ ] build.gradle.kts 中 `okhttp3` 依赖（`VoiceWsClient` 是唯一 WS 使用者；若后续拉会话接口用 OkHttp 则保留——建议保留 OkHttp 作为 REST client，`VoiceWsClient` 删除即可）

### 4.2 新增

- [ ] `net/RtcClient.kt`（TRTC 封装，接口见 §2）
- [ ] `net/VoiceSessionApi.kt`（REST 拉取 roomId+userSig：`POST /api/v1/voice/session`，OkHttp 即可，带缓存与过期刷新）
- [ ] `config/VoiceConfig` 增加 TRTC 配置项（SDKAppID、会话接口 URL）
- [ ] build.gradle.kts 增加 `LiteAVSDK_TRTC` 依赖（锁版本）+ ndk abiFilters + proguard 规则
- [ ] AndroidManifest.xml 增加权限（§1.2）

### 4.3 修改

- [ ] `voice/VoiceForegroundService.kt`：triggerWake / endUplink / onDestroy 走 RtcClient + 会话 API，移除 LAN/RELAY 分支
- [ ] `voice/FrameDispatcher.kt`：去掉上行分支，保留 KWS/RMS/VAD 钩子
- [ ] `config/VoiceConfig.kt`：连接模式迁移（conn_mode/lan/relay 相关标记废弃，或走 `migrateIfNeeded` 版本升级清理）

### 4.4 保留（不动）

- `MainActivity.kt` / `SettingsActivity.kt` / `ui/FloatingOverlay.kt` / `ui/WaveformView.kt`
- `voice/VoiceController.kt` / `voice/VoiceState.kt`（状态总线 + 六态）
- `voice/MicRecorder.kt`（监听阶段采集）/ `voice/WakeWordEngine.kt`（KWS）/ VadEngine（预留）
- `voice/VoiceForegroundService.kt` 的前台服务/通知/悬浮窗骨架
- `JaxApp.kt`

---

## 5. 选型记录（TRTC 最终选型，Agora 否决）

> 架构已定稿（ARCHITECTURE.md §2）：**TRTC 加权 4.6 vs Agora 3.6**。决定性差异 = Windows 端实时对端承载（TRTC Electron SDK 官方支持；Agora 实时 Python SDK 仅 Linux/macOS）+ CloudBase 同生态 + 国内节点延迟。Agora 不再接入。

| 维度 | TRTC（选定） | Agora（否决） |
|---|---|---|
| 依赖写法 | `com.tencent.liteav:LiteAVSDK_TRTC:13.4`（锁版本） | `cn.shengwang.rtc:voice-sdk`（不采用） |
| Windows 实时对端 | ✅ TRTC Electron SDK（be-pc sidecar） | ⚠️ 仅 Windows C++/C# SDK |
| Python 实时客户端 | ❌ 仅服务端管理 API（配合 sidecar 用 GenUserSig） | ⚠️ 有实时 Python SDK 但不支持 Windows |
| 纯音频场景 | `TRTCAppSceneAudioCall` | 支持（不采用） |
| 加密 | DTLS-SRTP 默认；私有加密付费可选 | enableEncryption 免费（不采用） |
| 自动重连 | 无限重连 onConnectionLost/TryToReconnect/Recovery | onRejoinChannelSuccess（不采用） |
| 音量回调 | `onUserVoiceVolume`（0~100，userId 空=本地） | onAudioVolumeIndication（不采用） |
| 生态 | 腾讯云（与 CloudBase 同账号/SecretKey 体系） | 独立账号体系 |
| 免费额度 | 10k 分钟/月 × 第一年（1v1 计 2×） | 10k 分钟/月（永久循环） |

**E2EE 结论（TRTC）**：默认 DTLS-SRTP 传输加密 + UserSig 鉴权满足本项目边界（录音不出必要范围、不落盘不进日志）；媒体流私有加密为付费能力，非硬需求不启用。`VoiceCipher` 应用层加密删除。

---

## 6. 待办与依赖（跨角色，按架构 Phase A/B/C）

- [x] **架构师**：✅ TRTC SDKAppID=1600155678（SecretKey 在项目根 .env，禁止进文档/git）；ADR-012 全量 Accepted；会话签发裁决 = **云函数代签**（ARCHITECTURE.md §3.4）
- [ ] **be-pc**：CloudBase/SCF 云函数 `trtc-sign`（HTTP 触发器，公网可达）实现 `POST /api/v1/voice/session` 代签 + PC 端会话意图轮询；PC sidecar 进房（哑对端先验证）
- [ ] **fe-mobile（本端，Phase A）**：按本方案实现 RtcClient + VoiceSessionApi，接入 triggerWake/endUplink；用官方 Demo 哑对端验证双向音频
- [ ] **qa**：验收对齐 QA-PLAN —— 进房 ≤2s、断网自动重连（场景 A/B）、强杀重进房幂等（场景 C）、六态+悬浮窗不退化、E2EE/越权测试（场景 §6.3）
- [ ] **qa（打断口径，已同步）**：验收「Speaking 中说停 <500ms」判定来源从本地 mic VAD 改为 SDK 播放/远端音频状态（§3.4），测试口径与手机端一致
- [ ] **本端实现**：`RtcClient.kt` 具体实现（本文不写实现代码）
