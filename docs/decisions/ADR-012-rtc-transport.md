# ADR-012: 语音传输层 — 自研 WS 中继重构为腾讯云 TRTC（实时音视频）

- 状态：**Accepted（2026-08-06 全量，team-lead 裁决）** —— pending: user credentials 已消除（SDKAppID=1600155678 确认、SecretKey 在项目根 .env）；会话签发方案已裁决（云函数代签，见决策 #7 与变更记录）
- 日期：2026-08-06
- 决策者：架构师 高见远（与 fe-mobile / be-pc / qa 对齐后，team-lead 裁决）
- 关联：docs/rtc-rebuild/ARCHITECTURE.md（本文档是架构决策的 ADR 沉淀）、ADR-003-voice-pipeline.md（模型原生全双工保留）、mobile-voice-spec.md（V1.5 语音主线）

## 背景

自研三层 WS 中继链路（`backend/relay/relay_server.py + relay_client.py` + 手机 `VoiceWsClient.kt` WS 状态机）经 100 轮修复仍不稳定：配对卡住、重连风暴、心跳协议错位、中继假死。用户拍板推倒重构为成熟 RTC 方案。MiniCPM-o Realtime API 全双工能力为保留资产（`apm_bridge.py`），本次只换传输层。

## 选项对比（2026-08 官方文档核查）

| 维度 | TRTC（腾讯云） | Agora（声网） |
|---|---|---|
| Android SDK | 成熟，`TRTCAppSceneAudioCall` 纯语音场景原生 | 成熟 |
| **Windows 实时对端** | **官方 Electron SDK（trtc-electron-sdk，Node.js，支持 Windows x64）** | 仅 Windows C++/C# SDK |
| **Python 实时客户端（Windows）** | 无（`tencentcloud-sdk-python-trtc` 仅服务端管理 API，不能进房收发音频） | **无**（`agora-python-server-sdk` 是实时客户端但仅 Linux/macOS） |
| 免费额度 | 10k 分钟/月 × 第一年（1v1 计 2× 时长） | 10k 分钟/月（永久循环，同 2× 时长） |
| 与 CloudBase 同生态 | **同为腾讯云：同账号/SecretKey/UserSig 体系** | 独立体系，无协同 |
| 国内延迟 | 腾讯骨干，官方宣称 <300ms | SD-RTN 全球节点，国内亦覆盖 |

**结论**：推荐 **TRTC**。决定性差异为 Windows 端实时承载（TRTC 有官方 Electron SDK）+ CloudBase 同生态。Agora 否决。

**架构偏差声明**：TRTC 与声网在 Windows 上均无官方 Python 实时客户端 SDK，故采用「**Node.js RTC sidecar + Python 大脑**」：实时音频对端由 TRTC Electron SDK（Node.js 进程）承担，Python FastAPI 保留全部业务逻辑（会话 / apm_bridge / 大脑）。这是对"Python 常驻对端"的最小偏差，也是 Windows 上最成熟路径。

## 决策

1. **传输层选 TRTC**（不用 Agora）；会话场景 `TRTCAppSceneAudioCall`（纯语音）。
2. **端到端链路**：手机 TRTC Android SDK ↔ TRTC 云 ↔ PC sidecar（trtc-electron-sdk）↔ 本地 WS（16k s16 PCM）↔ `rtc_bridge.py` ↔ `apm_bridge.py`（MiniCPM-o，保留）↔ MiniCPM-o Realtime API。
3. **仅会话期进房**：手机 KWS 唤醒 → `POST <云函数>/api/v1/voice/session` 拉 `room_id + user_sig` → `enterRoom` → 对话 → 静默超时/结束 → `exitRoom`。常驻监听（唤醒词）不消耗 RTC 分钟。
4. **删除自研 WS 链路**：backend/relay/ 全部、deploy/relay/、手机 VoiceWsClient/FrameCodec/PairFrame/VoiceCipher、ws_server.py（局域网直连统一走 TRTC）；relay 相关 38 个测试用例**显式迁移**为 RTC 对端等价用例（不静默删，对齐 QA-PLAN §6 反作弊门）。
5. **保留**：apm_bridge.py、session.py apm 桥接、half_duplex.py（降级链）、MicRecorder/AudioTrack 采集播放、大脑管线、其余 256 个单测（一个不动）。
6. **版本锁定**（双端都锁，禁 latest.release）：
   - Android：`com.tencent.liteav:LiteAVSDK_TRTC:13.4`（mavenCentral 精确版本写死，2026-06 稳定线）
   - PC sidecar：`trtc-electron-sdk` 精确版本（与手机端 13.4 对应稳定线，be-pc 落实）
7. **会话签发与进房协调 = 云函数代签（方案 A，2026-08-06 裁决）**：手机（深圳公网）直调 CloudBase/SCF 云函数 `trtc-sign` 的 `POST /api/v1/voice/session` 拿 userSig；PC（衡阳 NAT 后，无公网入站）不提供签发端点，由 PC 主动外呼轮询会话意图 → 取自身 userSig → 进同一房间。**SecretKey 唯一存放于云函数环境变量**（`TRTC_SECRETKEY`），PC .env 生产路径置空、手机 App 不持有。房间号规则：`room_id = TRTC_ROOM_PREFIX + device_id`（`TRTC_ROOM_PREFIX` 环境变量 = `jax-`）。详见 ARCHITECTURE.md §3.4。

### 手机端细节（fe-mobile 确认，ADR 固化防歧义）

1. **mic handoff（麦克风独占交接）**：监听阶段 `MicRecorder`(AudioRecord 16k) + KWS 常驻采集；会话期 mic 被 TRTC SDK 独占 → MicRecorder stop，打断/波形改用 SDK 回调（`onRemoteUserAudioStatus` + 播放状态），不依赖本地 VAD。这是会话期进房模式的必要前提（Android 不允许双 AudioRecord 同时采集）。
2. **版本锁定**：Android `LiteAVSDK_TRTC:13.4` 写死；sidecar `trtc-electron-sdk` 同锁。
3. **会话契约**：手机端 KWS 唤醒 → `POST <云函数>/api/v1/voice/session`（body `{device_id}`；pairing_code 语义废弃可省）→ 返回 `{room_id, user_id, user_sig, sdk_app_id, scene}`（**wire 层统一 snake_case**，手机 Kotlin 侧映射 camelCase）；userSig 短时效 ≤600s；SecretKey 唯一存云函数环境变量；房间号规则 `room_id = TRTC_ROOM_PREFIX + device_id`（`jax-<device_id>`），同 device 幂等复用房间。

## 后果

- **正面**：
  - 心跳/配对/重连由 SDK 内置，状态空间大幅收敛（10+ 控制帧 → 只剩会话开始/结束）。
  - 国内跨省延迟有腾讯云骨干保障；断线重连目标 ≤5s（SDK 自动），优于旧中继 ≤30s。
  - 与 CloudBase 同账号体系，userSig 签发可平滑迁移到云函数。
  - 免费额度 10k 分钟/月×1 年：仅会话期进房，预估月用量 ~1800 分钟，个人可控。
- **负面**：
  - PC 端新增 Node.js sidecar 组件 → 需进程守护（Python 拉起 + 心跳重启），运维面增加。
  - 依赖腾讯云 TRTC 服务可用性；免费额度第一年，长期需评估 1v1 套餐成本。
  - 传输层加密由 TRTC 承担，应用层 E2EE（VoiceCipher）废弃——媒体流经 TRTC 编码，无法应用层加密（录音仍不落盘、不进日志）。
  - 会话期 barge-in 判定来源从本地 mic VAD 变为 SDK 播放/远端音频状态（mic handoff），打断验收口径随 QA-PLAN 调整。

## 待裁决项（team-lead 2026-08-06 已裁决）

1. ~~用户腾讯云账号是否已开通 TRTC / 可创建 SDKAppID / 可获取 SecretKey？~~ → **✅ 已确认（2026-08-06）**：SDKAppID=**1600155678**，SecretKey 已在项目根 `.env`（`TRTC_SECRETKEY`，**禁止进文档/git/日志；本文档与架构文档一律不出现明文**）。云函数环境变量为生产唯一持有方（决策 #7）。此条消除，ADR 转全量 Accepted。
2. ~~手机播放 MVP 走 TRTC SDK 自动播放？~~ → **✅ 同意**：MVP 走 SDK 自动播放，AudioTrack 保留不参与（后续需波形/打断再注册 onAudioFrame 回调切换）。
3. ~~局域网直连统一走 TRTC、不保留自研 ws_server？~~ → **✅ 同意**：统一单一链路，删除全部自研 WS，减少维护面。

## 实施补充（fe-mobile Phase A 核对，ADR 固化防歧义）

- 回调名以官方 `TRTCCloudListener` 为准：`onTryToReconnect`（非 onTryReconnect）、`onConnectionLost` / `onConnectionRecovery`、音量回调 `onUserVoiceVolume`、远端音频状态 `onRemoteUserAudioStatus`；13.4 实际签名以 SDK jar 为准。
- `TRTCParams`：`strRoomId` 与 `intRoomId` 互斥（用 strRoomId 时 intRoomId 必须为 0）。
- 退出时序：`exitRoom` 需等 `onExitRoom` 回调后再重启 MicRecorder，避免 mic 抢占竞态。

## 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-08-06 | 全量 Accepted（消除 pending: user credentials） | SDKAppID=1600155678 确认、SecretKey 在项目根 .env；ADR 状态由「Accepted（带 pending）」升为全量 Accepted |
| 2026-08-06 | 决策 #7 新增：会话签发与进房协调 = 云函数代签（方案 A） | 手机直调云函数拿 userSig；PC 轮询会话意图取自身 userSig 进同房；SecretKey 唯一存云函数环境变量；房间号规则 TRTC_ROOM_PREFIX+device_id |
| 2026-08-06 | 四文档对齐修正（ARCHITECTURE/MOBILE/PC/QA-PLAN vs ADR-012） | ①会话契约 wire 层统一 snake_case（room_id/user_id/user_sig/sdk_app_id/scene）②手机 userId=device_id（PC-INTEGRATION 原 "pc-phone" 定值修正）③房间号统一 TRTC_ROOM_PREFIX+device_id ④.env 变量名统一 TRTC_SDKAPPID/TRTC_SECRETKEY/TRTC_ROOM_PREFIX（原文档 TRTC_SDK_APP_ID/TRTC_SECRET_KEY 修正）⑤删除清单补 backend/app/voice/e2ee.py 与 routes_voice.py 的 /ws/voice、/api/v1/voice/stream、/api/v1/voice/pair 端点 ⑥SecretKey 保管从「只存 PC .env」改为「唯一存云函数环境变量（PC 生产置空）」⑦QA-PLAN §5.1 状态字段 "relay=rtc-connected" → "rtc_status=connected" ⑧ARCHITECTURE §3.3 打断行「手机侧 VAD 停播」→「TRTC 播放/远端音频状态停播」（对齐 §5.1 mic handoff）；AUDIT.md D1/D2/D6 审计口径同步 ⑨PC-INTEGRATION §0/§4.4/§7、MOBILE-INTEGRATION §1.3/§1.5 残留旧签发描述修正 |
| 2026-08-06 | QA 审计 P1 复核：契约字段漂移清理 | 裁决 **wire 层 snake_case 定案**；ADR-012 决策 #3 正文残留 "roomId+userSig" 示例改为 "room_id + user_sig"。说明：TRTC 官方概念名 userSig（token）与 SDK API 名 TRTCParams.strRoomId/intRoomId 保留原文，不属于 wire 字段。变更记录本身不含 camelCase 残留示例 |

## 关联 ADR

- ADR-003-voice-pipeline.md：模型原生全双工保留，本决策只换传输层。
- ADR-011（如存在）：手机语音架构升格（mobile-voice-spec 决策）。
