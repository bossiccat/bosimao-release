# ARCHITECTURE — 自研 WS 中继重构为成熟 RTC 方案（波斯猫语音）

> 版本：v1.1（2026-08-06，会话签发与进房协调裁决 + 四文档对齐终检）
> 作者：architect（首席架构师）
> 状态：**已定稿**（team-lead 2026-08-06 裁决：TRTC 选型 + 云函数代签方案）
> 依据：docs/PRD.md、docs/specs/mobile-voice-spec.md（V1.5 语音主线）、docs/rtc-rebuild/QA-PLAN.md（QA 验收标准，本文档与之对齐）、docs/decisions/ADR-012-rtc-transport.md（本架构的 ADR 沉淀，Accepted）、后端 relay 链路现状（backend/relay/ + mobile-app VoiceWsClient）
> 决策类型：ADR-012（RTC 传输层选型 + 会话签发协调，Accepted）

---

## 0. TL;DR（30 秒结论）

**选型结论：腾讯云 TRTC（实时音视频），不用声网 Agora。**

- **手机端**：TRTC Android SDK（Kotlin 集成），`TRTCAppSceneAudioCall` 纯语音通话场景。
- **电脑端**：TRTC Electron SDK（Node.js sidecar 进程，Windows 原生支持）→ 本地 WebSocket → Python FastAPI + `apm_bridge.py`（MiniCPM-o 桥，保留）。
- **为什么不是纯 Python RTC 对端**：TRTC 与声网**都没有**「Windows 上可用的官方 Python 实时客户端 SDK」——TRTC 的 Python SDK 只是服务端管理 API（不能进房收发音频）；声网的 Python Server SDK 是实时客户端但**仅支持 Linux/macOS**。在 Windows PC 上，TRTC 有官方 Electron SDK 可承载实时音频对端，这是最小偏差路径。
- **删除**：backend/relay/ 全部、deploy/relay/ 全部、手机端 VoiceWsClient.kt / FrameCodec.kt / PairFrame.kt / VoiceCipher.kt、4 个 relay 测试文件（38 用例**显式迁移**为 RTC 对端等价用例，不静默删除，对齐 QA-PLAN §6 反作弊门）。
- **保留**：apm_bridge.py、session.py 的 apm 桥接、half_duplex.py（降级链）、AudioRecord 采集、AudioTrack 播放、大脑 intent 管线、PushManager、其余 256 个单测（一个不动）。
- **迁移**：Phase A 手机端换 RTC → Phase B 电脑端 RTC 对端 → Phase C 联调 + 删 relay。每阶段有验收点。

---

## 1. 背景与问题

### 1.1 现状

当前语音链路为自研三层 WS 中继：

```
手机 App(Kotlin, VoiceWsClient) → 自研 WS 中继(relay_server.py + relay_client.py)
    → 语音网关(session.py, path=apm) → MiniCPM-o Realtime API
```

100 轮修复仍存在：配对卡住（peer_left 重发循环）、重连风暴（旧连接 onClosed 触发新连接顶掉旧连接）、心跳协议错位（ping/pong/heartbeat 三套语义互踢）、中继假死（连上但无响应）。**用户已拍板：推倒重构为成熟 RTC 方案。**

### 1.2 保留资产（重构只换传输层，能力不能退化）

- **MiniCPM-o Realtime API**（`wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio`）：匿名可用，16k f32 上行、24k f32 下行，全双工随时打断。桥接实现在 `backend/app/voice/apm_bridge.py`（**保留复用**）。
- **路径 B 降级链**（half_duplex.py：sherpa STT → brain intent → TTS）。
- **手机端采集/播放/唤醒**：MicRecorder（AudioRecord 16k）、WakeWordEngine（sherpa KWS）、AudioTrack 播放。
- **294 个单元测试**（relay 相关 38 个迁移，其余 256 个不动）。

---

## 2. RTC 选型矩阵：TRTC vs Agora

### 2.1 决策前提（影响矩阵的硬约束）

1. **PC 端是 Windows 且后端是 Python**（FastAPI + apm_bridge 常驻）。RTC 实时对端必须跑在这台 Windows PC 上（不能要求云服务器，否则"电脑端常驻对端"模型不成立）。
2. **手机端是 Android/Kotlin**，需要成熟的 Android 实时音频 SDK。
3. **已连腾讯云 CloudBase**（同生态协同权重高）。
4. **纯音频通话**（AudioCall）场景，不需要视频。
5. **个人项目，免费额度要够用**。

### 2.2 关键事实核查（2026-08 官方文档）

| 事实 | TRTC（腾讯云） | Agora（声网） |
|------|----------------|---------------|
| Android SDK | ✅ `com.tencent.liteav:LiteAVSDK_TRTC`，mavenCentral 发布，`TRTCAppSceneAudioCall` 纯语音场景原生支持（48kHz 双声道） | ✅ 成熟，Voice Calling 支持 |
| **Windows 实时对端** | ✅ **TRTC Electron SDK**（`trtc-electron-sdk`，Node.js，支持 Windows x64/ia32）；另有 Windows C++ SDK | ⚠️ 官方仅 Windows C++/C# SDK |
| **Python 实时客户端** | ❌ `tencentcloud-sdk-python-trtc` 仅为**服务端管理 API**（房间管理/录制/AI 对话调度），**不能进房收发音频** | ⚠️ `agora-python-server-sdk` 是实时客户端（可进房、PushAudioPcmData、音频回调），**但仅支持 Linux/macOS，不支持 Windows** |
| 免费额度 | ✅ 每账号每月 **10,000 分钟**免费（通话/直播/录制等抵扣），第一年；超出后音频约 $0.99/1000 分钟（≈¥7/1000 分钟） | ✅ 每账号每月 **10,000 分钟**免费（永久循环）；超出 $0.99/1000 分钟 |
| 计费口径 | 按音视频时长（用户数 × 在房时长）；1v1 通话 M 分钟 = 2×M 分钟 | 按用户数（N 用户 × M 分钟）；1v1 同样 = 2×M 分钟 |
| 国内可用性/延迟 | ✅ 腾讯云自有节点，国内覆盖好，官方宣称端到端 <300ms | ✅ 声网 SD-RTN 全球节点，国内亦覆盖 |
| 与 CloudBase 同生态 | ✅ **同为腾讯云**：同一账号/控制台/账单/SecretKey 体系（UserSig 用 SecretKey 计算），可对接 CloudBase 函数签发 | ❌ 独立账号体系，无协同 |
| 传输安全 | ✅ DTLS-SRTP 加密 + 房间鉴权（UserSig） | ✅ 传输加密 + Token 鉴权 |
| 纯音频计费封顶 | 免费 10k 分钟/月够个人对话场景 | 同左 |

> 事实来源：腾讯云 TRTC 官方文档（产品页 / 计费说明 / Electron 集成文档 / Android 通话模式文档）；Agora 官方文档（Voice Calling Python Quickstart / Billing Policies / GitHub Agora-Python-Server-SDK Release Notes）。

### 2.3 评分矩阵（权重：学习成本 高 / 生态成熟度 高 / 部署成本 高 / 扩展性 低 / 团队熟悉度 高）

| 维度（权重） | TRTC | Agora | 说明 |
|--------------|------|-------|------|
| Windows 端实时承载（高） | 5 | 2 | TRTC Electron SDK 官方支持 Windows；Agora Python Server SDK 仅 Linux/macOS，Windows 需 C++/ctypes 曲线，不成熟 |
| Android SDK 成熟度（高） | 5 | 5 | 均为头部厂商，纯语音场景都成熟 |
| Python/服务端 SDK（高） | 2 | 4 | TRTC Python 仅管理 API；Agora 有实时 Python SDK 但平台受限 |
| 免费额度够用（高） | 5 | 5 | 同为 10k 分钟/月 |
| 国内可用性/延迟（高） | 5 | 4 | 腾讯云国内节点覆盖与延迟略优（同腾讯骨干） |
| CloudBase 同生态（高） | 5 | 2 | 同账号体系、可复用现有腾讯云账号与 SecretKey 管理 |
| 纯音频 AudioCall 支持（高） | 5 | 5 | 都原生支持 |
| 团队熟悉度（中） | 3 | 3 | 两者团队都未用过，但腾讯云账号体系已熟悉 |
| 扩展性（低） | 4 | 4 | MVP 单用户 1v1，两者足够 |
| **加权总分** | **4.6** | **3.6** | — |

### 2.4 结论：推荐 TRTC

1. **Windows 端承载是决定性差异**：PC 端必须能在 Windows 上跑实时音频对端。TRTC 有官方 Electron SDK；Agora 的实时 Python SDK 不支持 Windows，唯一"纯 Python"路径是部署到 Linux 云服务器或 WSL——都偏离"电脑端常驻对端"模型，且增加一跳与运维复杂度。
2. **与 CloudBase 同生态**：项目已连腾讯云 CloudBase，TRTC 与 CloudBase 同一账号体系，UserSig 签发、控制台、账单统一；**userSig 签发已裁决为 CloudBase/SCF 云函数代签**（§3.4）。
3. **国内可用性与延迟**：深圳手机 ↔ 衡阳电脑为跨省公网，腾讯云国内节点覆盖与骨干质量是强项。
4. **免费额度**：10k 分钟/月第一年。按"仅会话期进房"设计（见 §4.4），个人月对话量远低于额度。

**明确否决 Agora**：非 Windows 端 Python 实时 SDK + 无 CloudBase 生态协同，不满足本项目两个关键前提。

> **诚实声明（架构偏差）**：任务描述为"电脑端(Python+RTC SDK 常驻对端)"。经核查，TRTC/声网在 Windows 上**均无官方 Python 实时客户端 SDK**，故采用 **Node.js RTC sidecar + Python 大脑** 架构：实时音频对端由 TRTC Electron SDK（Node.js 进程）承担，Python FastAPI 仍负责全部业务逻辑（会话、apm_bridge、大脑）。这是对"Python 常驻对端"的最小偏差，也是 Windows 上最成熟的路径。若后续 PC 迁移到 Linux，可无缝替换为 Agora Python Server SDK（接口层抽象见 §5.2 的 RtcPeer 契约）。

---

## 3. 端到端架构图

```
┌─────────────────────────────┐         ┌──────────────────────────────────────────┐
│        手机端 Android App     │         │             电脑贾克斯（Windows）           │
│                             │         │                                          │
│  ┌───────────────────────┐  │ ① 进房  │  ┌──────────────────────────────────┐   │
│  │ VoiceForegroundService│  │ 音频上行 │  │ RTC sidecar (Node.js/Electron)     │   │
│  │  ├ MicRecorder         │──┼────────▶│  │  └ trtc-electron-sdk 实时对端       │   │
│  │  │   (AudioRecord 16k)│  │   ② RTC │  │     进房(AudioCall) / 音频回调       │   │
│  │  ├ WakeWordEngine(KWS)│◀─┼─────────│  │     本地 WS 桥 (ws://127.0.0.1:port) │   │
│  │  ├ VadEngine(bargein) │  │ 音频下行│  └───────────────┬──────────────────────┘   │
│  │  ├ RtcClient(TRTC)    │  │         │                  │ ③ 本地 WS（16k s16 PCM）  │
│  │  │  AudioCall 进房     │  │         │  ┌───────────────▼──────────────────────┐  │
│  │  │  startLocalAudio    │  │         │  │ Python FastAPI（backend）             │  │
│  │  └ AudioTrack 播放     │  │         │  │  ├ voice/rtc_bridge.py（新增，RtcPeer）│  │
│  ├ ui/FloatingOverlay    │  │         │  │  ├ voice/session.py（apm 桥接保留）     │  │
│  └ config/VoiceConfig    │  │         │  │  ├ voice/apm_bridge.py（保留）          │  │
└────────────┬──────────────┘  │         │  │  └ voice/half_duplex.py（降级链保留）   │  │
             │                 │         │  └──────────────┬──────────────────────┘  │
             │                 │         │                 │ ④ 16k s16 PCM           │
             │        ┌────────▼─────────▼───────┐         │ ⑤ 24k f32 下行           │
             │        │  TRTC 实时音频云（SD-RTN）│         └────────────┬─────────────┘
             │        │  房间 roomId + UserSig    │                      │
             │        └────────▲─────────▲───────┘         ┌────────────▼─────────────┐
             │                 │         │                 │ MiniCPM-o Realtime API    │
             │  ⑥ 控制面（低频 REST）     │                 │ wss://minicpmo45.../audio  │
             └─────────────────┴─────────┴─────────────────│ apm_bridge 全双工保留       │
                                                           └──────────────────────────┘
```

### 3.1 数据流（一句话）

`手机 Mic(16k s16) → TRTC SDK 编码 → SD-RTN → PC sidecar 解码 → 本地 WS(16k s16) → rtc_bridge → apm_bridge 累积 1s 块 → f32 base64 → MiniCPM-o → 24k f32 下行 → f32_to_s16_16k → 本地 WS → sidecar → TRTC 上行 → 手机解码 → AudioTrack 播放`

### 3.2 音频流转与格式转换点

| 段 | 源格式 | 转换点 | 目标格式 | 说明 |
|----|--------|--------|----------|------|
| 手机采集 → RTC | 16k s16 PCM（AudioRecord） | TRTC SDK 内部（编码 Opus） | 48k 编码流（SD-RTN） | 手机端 `TRTC_AUDIO_QUALITY_SPEECH`（语音档，16k）可减少重采样 |
| SD-RTN → PC sidecar | 编码流 | TRTC SDK 内部（解码） | 48k f32 PCM 回调 | sidecar 的 `onAudioFrame` 回调 |
| sidecar → Python | 48k f32 | **sidecar 内重采样**（或 rtc_bridge） | 16k s16 PCM（本地 WS 统一格式） | 本地 WS 统一用 16k s16，与 apm_bridge 上行格式一致，链路零额外转换 |
| Python → apm_bridge | 16k s16 PCM | `apm_bridge.feed_pcm`（累积 1s 块 → f32 → base64） | MiniCPM-o input.append | **保留现有实现** |
| MiniCPM-o → Python | 24k f32 base64 | `apm_bridge.f32_to_s16_16k`（24k→16k 线性抽取 + int16 量化） | 16k s16 PCM | **保留现有实现** |
| Python → sidecar | 16k s16 PCM | 本地 WS 透传 | 16k s16 PCM | 零转换 |
| sidecar → RTC | 16k s16 PCM | TRTC SDK 内部（编码） | 48k 编码流 | `startLocalAudio` 语音档 |
| RTC → 手机播放 | 编码流 | TRTC SDK 内部（解码 + AudioTrack） | 16k/48k PCM | 手机端由 SDK 解码播放（或回调 PCM 走现有 AudioTrack 播放器） |

> **原则**：本地 WS 桥统一 16k s16 PCM（与现有协议/测试基线一致）；所有采样率转换集中在两个边界——sidecar（RTC 回调 → 16k）与 apm_bridge（24k f32 → 16k s16，已存在）。

### 3.3 控制面（会话开始/结束）设计

RTC 只承载**音频流**。会话控制（低频、非实时）走现有 FastAPI 控制面 REST，**不发明新的实时控制协议**：

| 控制动作 | 通道 | 说明 |
|----------|------|------|
| 手机唤醒 → 请求会话 | `POST <云函数>/api/v1/voice/session`（云函数代签，替代旧配对接口） | 手机 KWS 命中 → 云函数签发凭证 + 写会话意图；PC 轮询意图进房（§3.4） |
| 手机请求会话 → 签发凭证 | 手机 KWS 唤醒 → `POST <云函数>/api/v1/voice/session`（§3.4） | UserSig 由**云函数代签**（SecretKey 唯一存云函数环境变量），不入手机 App、不入 PC 生产路径 |
| 手机进房 | 手机用返回的 roomId + userSig 进 TRTC 房间 | 进房即开始音频全双工 |
| 会话结束 | 任一端 `exitRoom`（VAD 静默 + 回复结束 15s 超时） | 双方退房，RTC 分钟停止计费 |
| 打断 | **无需显式控制帧** | MiniCPM-o 全双工：手机持续上行音频，用户开口即模型原生 barge-in；手机侧按 TRTC 播放/远端音频状态停播下行（本地兜底；mic handoff 后不依赖本地 VAD，见 §5.1） |
| 状态同步（六态） | 复用现有 EventBus / 推送 | session_state 经现有 WS/推送下行到手机悬浮窗 |

> 相比自研 relay 的 10+ 种控制帧（wake/speech_start/speech_end/interrupt/cancel/heartbeat/ping/pong/paired/peer_left），RTC 方案**删除全部心跳与配对协议**（SDK 内置），控制帧只剩"会话开始/结束"，大幅收敛状态空间。

### 3.4 会话签发与进房协调（公网可达裁决：云函数代签）

**问题**：手机在深圳公网（4G），PC 在衡阳家庭宽带 NAT 后**无公网入站**。手机需调 `POST /api/v1/voice/session` 获取 userSig 才能进房；若签发端点挂在 PC 后端，手机无法直达。

**裁决（team-lead 2026-08-06）**：**方案 A — 云函数代签**。UserSig 签发部署到腾讯云 CloudBase/SCF 云函数（服务 `trtc-sign`，HTTP 触发器公网可达），手机直调云函数；PC 由主动外呼轮询协调进房。**全程不依赖 PC 公网可达**。

| 项 | 结论 |
|---|---|
| **签发部署位置** | CloudBase/SCF 云函数 `trtc-sign`；`GenUserSig`（HMAC-SHA256，纯 Python）在云函数内实现；**SecretKey 唯一存放于云函数环境变量 `TRTC_SECRETKEY`**（另含 `TRTC_SDKAPPID` / `TRTC_ROOM_PREFIX`），不进 PC .env 生产路径、不进手机 App、不进 repo/日志 |
| **手机进房** | KWS 唤醒 → `POST <云函数>/api/v1/voice/session`（body `{device_id}`）→ 云函数校验 device 白名单 → 计算 room_id → 签手机 userSig（userId=device_id，expire ≤600s）→ 写"会话意图"记录（device_id/room_id/ts）→ 返回 `{room_id, user_id, user_sig, sdk_app_id, scene:"audio_call"}` → 手机 `enterRoom` |
| **PC 进房协调** | PC 无入站，**由 PC 主动外呼轮询**：PC 后端常驻轮询器每 ~2s `GET <云函数>/api/v1/voice/session/pending?device_id=`；发现未消费会话意图 → `POST <云函数>/api/v1/voice/session/sign`（body `{device_id, user_id:"jax-pc-sidecar"}`）取 PC 自身 userSig → 通知 sidecar 进同一 room_id → 标记意图已消费。手机可先入房等待，PC 通常 ≤2s 加入 |
| **房间号规则** | **`room_id = TRTC_ROOM_PREFIX + device_id`**（`TRTC_ROOM_PREFIX` 环境变量 = `jax-`，如 `jax-<device_id>`）；同 device 幂等复用房间；防枚举依赖 userSig 房间鉴权（无合法 userSig 无法进房），而非房间号不可猜 |
| **退房/清理** | 任一端 `exitRoom`；TRTC 房间末位用户退房云侧自动销毁；PC sidecar 收到 `onRemoteUserLeave` → 退房回待命（§5.2 房间生命周期不变） |
| **Phase A 过渡** | 本地冒烟允许 PC 用 .env `TRTC_SECRETKEY` 临时签发（GenUserSig 纯函数）；**Phase B 起生产路径统一云函数，PC .env 的 TRTC_SECRETKEY 置空** |

> 手机 ↔ 云函数（HTTPS 公网）、手机 ↔ TRTC 云、PC ↔ 云函数（HTTPS 出站）、PC ↔ TRTC 云（出站）——PC 全程无入站连接。

---

## 4. 删除清单 vs 保留清单

### 4.1 删除清单（自研 WS 链路，全部废弃）

| 文件 | 位置 | 处置 |
|------|------|------|
| relay_server.py | backend/relay/ | 删除（中继服务） |
| relay_client.py | backend/relay/ | 删除（PC 中继客户端） |
| relay_protocol.py | backend/relay/ | 删除（帧协议/E2EE/防重放） |
| config.py | backend/relay/ | 删除 |
| __init__.py | backend/relay/ | 删除（包清空后移除目录） |
| deploy/relay/（整个目录） | deploy/relay/ | 删除（中继部署产物：Dockerfile / relay 快照） |
| VoiceWsClient.kt（WS 状态机） | mobile-app/.../net/ | 删除，替换为 RtcClient.kt（TRTC 封装） |
| FrameCodec.kt（二进制帧编解码） | mobile-app/.../net/ | 删除（RTC 接管音频帧） |
| PairFrame.kt（配对帧） | mobile-app/.../net/ | 删除（配对语义废弃） |
| VoiceCipher.kt（应用层 E2EE） | mobile-app/.../crypto/ | 删除（RTC 传输层加密替代；应用层 E2EE 无法作用于编码后媒体流） |
| FrameCodecTest.kt / VoiceCipherTest.kt | mobile-app/.../test/ | 删除（随实现删除） |
| test_relay_protocol.py / test_relay_server.py / test_relay_client_fake_dead.py / test_relay_client_gateway_heartbeat.py | backend/tests/unit/ | **38 个用例显式迁移**为 RTC 对端等价契约用例（进房/退房/重连/对端离线/超时清理），不静默删除（对齐 QA-PLAN §6 反作弊门） |
| e2ee.py（语音网关 E2EE 装配） | backend/app/voice/ | 删除（应用层 E2EE 废弃，RTC 传输层加密替代；与 VoiceCipher.kt 同理，relay_protocol 的 RelayE2EE 一并清理） |
| `/ws/voice`、`/api/v1/voice/stream`、`/api/v1/voice/pair` 端点 | backend/app/api/routes_voice.py | 删除（手机 WS 直连/局域网直连/配对统一走 TRTC；routes_voice.py 保留 status，新增 session） |
| .env 中 `RELAY_TOKEN` / `RELAY_E2EE_KEY` / `VOICE_TOKEN` | 项目根 .env | 清理（relay 残留；VOICE_TOKEN 随手机 WS 端点删除不再需要） |

> 依赖检查：backend/relay/ 被 backend/app/api/routes_voice.py（status 字段）、backend/app/voice/config.py、deploy/relay/ 引用——重构后这些引用一并清理。原 spec 的"局域网直连 ws_server.py"概念落地为 routes_voice.py 的 `/ws/voice` 与 `/api/v1/voice/stream` 端点（ADR-012 决策 #4：统一走 TRTC，删除）。

### 4.2 保留清单（能力资产，一个不能退化）

| 资产 | 位置 | 保留原因 |
|------|------|----------|
| apm_bridge.py | backend/app/voice/ | MiniCPM-o 全双工桥，核心保留资产 |
| session.py（apm 桥接路径） | backend/app/voice/ | send_apm_audio/text/state、path=apm 流式模式保留；WS 直连手机部分改造 |
| half_duplex.py | backend/app/voice/ | 路径 B 降级链（STT → brain → TTS） |
| audio.py / schemas.py（部分） | backend/app/voice/ | PCM 工具与 Pydantic 模型（音频帧 schema 可删，PCM 工具保留） |
| routes_voice.py | backend/app/api/ | 改造：配对码接口 → roomId/userSig 会话签发接口 |
| main.py | backend/app/ | lifespan 装配（改造为装配 RtcPeer） |
| MicRecorder / AudioTrack 播放 | mobile-app/.../voice/ | 采集与播放硬件层保留（RTC SDK 可接管播放，见 §5.1 决策点） |
| WakeWordEngine（sherpa KWS） | mobile-app/.../voice/ | 本地唤醒（唤醒前不进 RTC 房间） |
| FrameDispatcher 的 VAD 打断能力（M2 预留占位） | mobile-app/.../voice/ | 无独立 VadEngine.kt；VAD 为 FrameDispatcher 内 M2 占位，重构后按「barge-in 状态机驱动」落地，不新增协议（fe-mobile 确认） |
| VoiceController / VoiceState / 六态 UI / FloatingOverlay | mobile-app/... | 状态机与 UI 保留 |
| 其余 256 个单测 | backend/tests/unit/ | 一个不动 |
| 大脑 intent / 注入管线 / PushManager | backend/app/ | 与语音表达层解耦，保留 |
| MiniCPM-o Realtime API | 外部 | 保留 |

---

## 5. 组件设计要点（给 fe-mobile / be-pc 的接口契约）

### 5.1 手机端（fe-mobile 任务 #2）

- 新增 `RtcClient.kt`：封装 TRTC Android SDK 进房/退房/本地音频采集/远端音频播放/状态回调。
- 进房参数：`TRTCParams{sdkAppId, userId, userSig, strRoomId}` + `TRTCAppSceneAudioCall`。
- 采集：`startLocalAudio(TRTC_AUDIO_QUALITY_SPEECH)`（16k 语音档）；不调用 `startLocalPreview`（纯音频）。
- 播放决策点：**优先用 SDK 自动播放**（自动订阅模式下远端音频自动解码播放，最简单）；若需 VAD 打断/波形显示，注册 `onAudioFrame` 回调接管远端 PCM 走现有 AudioTrack 播放器（保留现有播放线程与 playGen 打断机制）。**MVP 建议先走 SDK 自动播放**，波形/打断用 `onRemoteUserAudioStatus` + 本地 VAD 做状态机驱动。
- 会话流程：KWS 唤醒 → REST 请求**云函数**签发会话（`POST <云函数>/api/v1/voice/session`，见 §3.4）→ 收到 roomId/userSig → `enterRoom` → 对话 → 静默超时/结束 → `exitRoom`。
- **麦克风独占交接（mic handoff，fe-mobile 关键设计）**：Android 同一 App 不能两个 AudioRecord 同时采集。监听阶段由 `MicRecorder` 常驻采集喂 KWS；唤醒命中 → 停 MicRecorder 释放 mic → TRTC SDK 自行采集上行；通话结束 `exitRoom` → 重启 MicRecorder 恢复"一直在听"。**架构影响**：会话期间本地 VAD/barge-in 不依赖 MicRecorder，改用 TRTC 音频回调（`onAudioFrame` / `onRemoteUserAudioStatus`）+ 播放状态机驱动打断。
- 断线：SDK 内置自动重连（TRTC 断网自动重进房），应用层只监听 `onConnectionLost/onTryReconnect/onConnectionRecovery` 驱动 UI 状态。

### 5.2 电脑端（be-pc 任务 #3）

新增两个组件：

1. **RTC sidecar（Node.js / Electron）**：
   - 进程：`pc-rtc-sidecar/`（独立 npm 工程，`trtc-electron-sdk` 锁定版本）。
   - 职责：进房（AudioCall）→ 远端音频回调 → 本地 WS 推 16k s16 PCM 给 Python；收 Python 下行 16k s16 PCM → TRTC 上行。
   - 本地 WS 契约（sidecar ↔ Python，仅本机回环，127.0.0.1）：
     ```
     sidecar→python: {type:"up_audio", pcm_b64}     # 手机音频（16k s16）
     sidecar→python: {type:"peer_state", state}      # 手机进/出房
     python→sidecar: {type:"down_audio", pcm_b64}    # 回复音频（16k s16）
     python→sidecar: {type:"ctrl", action:"exit"}    # 会话结束退房
     ```
   - 进程守护：Windows 下由 Python 后端拉起 + 心跳（sidecar 每 5s 上报，Python 超时 30s 重启）。

2. **Python `rtc_bridge.py`（RtcPeer 包装）**：
   - 对上：本地 WS 客户端连接 sidecar，接收 up_audio → `ApmBridge.feed_pcm`；接收 `ApmBridge.on_audio_out` 下行 → down_audio 回 sidecar。
   - 对下：依赖 `session.py` 的 apm 桥接回调（send_apm_audio/text/state）**原样复用**。
   - 会话管理（见 §3.4）：PC 侧为**会话意图轮询方**——常驻轮询云函数 `GET <云函数>/api/v1/voice/session/pending` 发现手机发起的会话意图 → `POST <云函数>/api/v1/voice/session/sign` 取 PC 自身 userSig（userId=`jax-pc-sidecar`）→ 通知 sidecar 进同一 room_id。**PC 不本地签发 userSig**（SecretKey 唯一存云函数环境变量；Phase A 本地冒烟可用 .env 临时签发，Phase B 起生产路径统一云函数）。
   - **RtcPeer 接口抽象**（对齐 QA-PLAN §4.1 单测）：`enter_room() / exit_room() / on_remote_audio(frame) / on_remote_user_enter/leave / on_connection_lost/recovery`。当前实现走 sidecar 本地 WS；若未来 PC 迁移 Linux，可替换为 Agora Python Server SDK 直连实现，接口不变。

### 5.3 格式转换（be-pc 交付物）

- sidecar 内：RTC 回调 PCM（48k f32）→ 重采样 → 16k s16（放 sidecar 侧，用 Node 的 `speexdsp` 或 `node-pcm` 类库；或统一由 Python `audio.py` 的 `resample_48k_to_16k` 完成——**推荐由 Python 完成**，sidecar 只透传原始回调，转换逻辑集中在 Python 可单测，见 QA-PLAN §4.1 变异点）。
- Python 内：`apm_bridge.f32_to_s16_16k`（24k f32 → 16k s16，已存在，保留）。

---

## 6. 迁移步骤（分阶段，每步有验收点）

### Phase A：手机端换 RTC（fe-mobile）

**范围**：手机端集成 TRTC Android SDK，RtcClient 进/退房、采集/播放；PC 侧用 TRTC 官方 Electron/Web Demo 作为"哑对端"进同一房间验证音频互通。

**步骤**：
1. 创建 TRTC 应用（控制台 SDKAppID，已确认 **1600155678**）；Phase A 本地冒烟用 PC .env 临时签发（GenUserSig 纯函数，见 PC-INTEGRATION 附录 A.1）；**云函数代签为生产路径（Phase B 起，§3.4）**。
2. 手机端接入 RtcClient，KWS 唤醒 → 拉会话 → 进房 → 上行采集 → 下行播放。
3. 用哑对端（官方 demo）验证：手机说话对端听到、对端说话手机听到。

**验收点**：
- [ ] 手机进房 ≤ 2s；音频双向互通（哑对端回声验证）。
- [ ] 手机断网 → SDK 自动重连 → 恢复后继续对话（对齐 QA-PLAN §2 场景 A）。
- [ ] 手机强杀 → 房间清理，重进房幂等（QA-PLAN §2 场景 C）。
- [ ] 六态状态机 + 悬浮窗不退化。
- [ ] apm_bridge 未改（本阶段不接）。

### Phase B：电脑端 RTC 对端（be-pc）

**范围**：PC 侧 sidecar + rtc_bridge + apm_bridge 闭环；会话签发接口；进程守护。

**步骤**：
1. 建 sidecar（npm 工程，锁 trtc-electron-sdk 版本），实现进房 + 本地 WS 桥。
2. Python 新增 rtc_bridge.py，接 apm_bridge（复用 session.py 的 apm 回调）。
3. 云函数 `/api/v1/voice/session` 代签 roomId + userSig（GenUserSig，SecretKey 唯一存云函数环境变量）+ PC 意图轮询。
4. 本地双端回环（QA-PLAN §4.2 方法 A：同机跑两个 RTC 客户端收发 WAV）+ 集成回环（§4.2 方法 B：RTC + apm_bridge + MiniCPM-o）。

**验收点**：
- [ ] 本地双端回环：A 发 WAV → B 收到完整音频（字节完整）。
- [ ] RTC + apm_bridge + MiniCPM-o 集成：首音、双轮、打断、停顿判定四项全过（QA-PLAN §3.2）。
- [ ] 手机(真机) ↔ PC sidecar ↔ MiniCPM-o 端到端闭环；首音 P50 ≤ 2.0s（手机端打点）。
- [ ] PC 断网 15s 恢复 → sidecar ≤ 10s 自动重进房（QA-PLAN §2 场景 B）。
- [ ] 进程守护：杀 sidecar → Python 30s 内拉起恢复。

### Phase C：联调 + 删 relay + 回归（all）

**范围**：删除清单全部执行；relay 38 用例迁移；全链路验收。

**步骤**：
1. 删除 backend/relay/、deploy/relay/、手机端 WS 相关文件（§4.1）。
2. 迁移 38 个 relay 用例为 RTC 对端等价用例（test_rtc_peer.py / test_audio_convert.py 等，QA-PLAN §4.4 落位）。
3. 清理 routes_voice.py / voice/config.py / main.py 中 relay 引用。
4. 全链路回归 + 跨网真机验收（QA-PLAN §5：深圳手机 4G ↔ 衡阳电脑）。

**验收点**：
- [ ] pytest tests/unit ≥ 294 全绿（256 保留 + 38 迁移 + 新增 RTC 用例；只增不减）。
- [ ] 删除清单逐项确认执行；`grep -rn "relay" backend/ mobile-app/` 无残留业务引用（文档/注释允许）。
- [ ] 跨网真机：首音 P50 ≤ 2.0s / P95 ≤ 3.0s；打断 < 500ms；30min 0 断开；静默 15s 回落（QA-PLAN §1/§5）。
- [ ] Android 单测全绿；视觉合规扫描（无 emoji 图标/无紫粉渐变）。
- [ ] 免费额度用量预估报告（见 §7 R1）。

---

## 7. 风险与缓解

| # | 风险 | 影响 | 缓解 | 责任人 |
|---|------|------|------|--------|
| R1 | **RTC 免费额度限制**（10k 分钟/月 × 第一年；1v1 计 2× 时长） | 额度用尽服务中断或产生费用 | ① **仅会话期进房**：KWS 唤醒后才进房，对话结束即退房，常驻监听不消耗 RTC 分钟（唤醒词本地跑）② 预估月用量：30min/天对话 × 2 端 × 30 天 ≈ 1800 分钟 << 10k ③ 用尽后音频约 ¥7/1000 分钟，个人可控 ④ 控制台告警 | be-pc |
| R2 | **SDK 版本锁定** | TRTC 迭代频繁，升级破坏 API/行为 | 锁定精确版本（Android `LiteAVSDK_TRTC` 具体版本号 + sidecar `trtc-electron-sdk` 具体版本）；版本号写入 ADR 与依赖锁文件；升级走回归门禁 | be-pc / fe-mobile |
| R3 | **跨网延迟波动**（深圳手机 4G ↔ 衡阳宽带 NAT 后） | 首音超时、卡顿 | TRTC 国内节点就近接入；家庭宽带 NAT 无公网 IP 不影响（UDP 穿透 + 云中继）；P95 ≤ 3s 硬门兜底；衡阳侧如网络差考虑 1v1 套餐/优化 QoS 参数 | be-pc / qa |
| R4 | **Windows 防火墙/UDP 限制** | RTC UDP 端口被封 → 无法进房 | sidecar 文档写明放行 UDP 常用端口；TRTC 支持 UDP 受限时降级（文档核对）；与现有 Clash 代理共存问题（显式绕过代理，对齐已知坑） | be-pc |
| R5 | **Node.js sidecar 增加组件** | 进程崩溃 / 版本管理复杂 | 进程守护（Python 拉起 + 心跳重启）；sidecar 保持极薄（只做 RTC ↔ 本地 WS 透传 + 格式透传，业务全在 Python）；npm 锁文件 | be-pc |
| R6 | **回声/啸叫**（手机免提 + 电脑外放） | 体验劣化 | TRTC AudioCall 默认开启 AEC/回声消除；真机免提 1m 场景实测（QA-PLAN R4） | qa |
| R7 | **音频格式不匹配**（RTC 回调 48k f32 → 16k s16） | 转换错 → 音频失真/卡顿 | 转换集中在 Python（可单测 + 变异加固）；sidecar 只透传原始回调 | be-pc |
| R8 | **UserSig 泄露** | 房间被越权进入 | SecretKey 唯一存云函数环境变量（PC .env 生产路径置空）；userSig 服务端签发、短有效期 ≤600s；房间鉴权依赖 userSig（无合法签名无法进房）；QA-PLAN §6.3 越权测试 | be-pc / qa |
| R9 | **apm_bridge 能力退化** | 重构传输层把全双工改坏 | 传输层与 apm_bridge 解耦；QA-PLAN §3.2 三项全双工回归（双轮/打断/停顿判定）硬门 | qa |
| R10 | **手机后台常驻功耗** | RTC 进房持续采集耗电 | 会话期进房模式（对话结束退房）；前台服务 + 电池白名单已有；对话期功耗实测（QA-PLAN §1.2） | fe-mobile / qa |

---

## 8. 待确认 / 开放问题（需 team-lead 裁决）

1. ~~TRTC 控制台开通~~ → **✅ 已确认（2026-08-06）**：SDKAppID=**1600155678**，SecretKey 已在项目根 `.env`（`TRTC_SECRETKEY`，禁止进文档/git/日志）；`.env.example` 补 TRTC 模板（be-pc Phase A P5）。云函数环境变量为生产唯一持有方（§3.4）。
2. **手机端播放路径**（§5.1）：MVP 用 TRTC SDK 自动播放（简单）vs 回调 PCM 走现有 AudioTrack（保留打断/波形控制）。建议 MVP 先 SDK 自动播放，打断由本地 VAD + `exitRoom/静音` 驱动。
3. ~~局域网直连是否保留~~ → **✅ 已裁决（ADR-012 #4）**：统一 TRTC 一条链路，不保留自研 ws_server（落地为删除 routes_voice.py 的 `/ws/voice`、`/api/v1/voice/stream`、`/api/v1/voice/pair` 端点，见 §4.1）。
4. **MiniCPM-o 隐私边界**：MiniCPM-o 为第三方云 API（保留链路，用户已知情）；TRTC 传输加密不改变该边界，文档需重申"录音不落盘、不进日志"。

---

## 9. 产出物清单（实施阶段）

| 产出物 | 归属 | 说明 |
|--------|------|------|
| `docs/decisions/ADR-012-rtc-transport.md` | architect | 本文档定稿后沉淀（选型 TRTC + sidecar 架构） |
| `backend/app/voice/rtc_bridge.py` | be-pc | RtcPeer 包装（本地 WS ↔ apm_bridge） |
| `pc-rtc-sidecar/`（npm 工程） | be-pc | TRTC Electron 对端 + 本地 WS 桥 |
| `mobile-app/.../net/RtcClient.kt` | fe-mobile | TRTC 手机端封装（替换 VoiceWsClient） |
| `backend/tests/unit/test_rtc_peer.py` + `test_audio_convert.py` | be-pc | RTC 层单测（QA-PLAN §4.4） |
| `docs/rtc-rebuild/`（本文档 + QA-PLAN） | all | 契约与验收 |
