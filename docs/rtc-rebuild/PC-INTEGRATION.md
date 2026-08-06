# 电脑端 RTC 对端 + MiniCPM-o 桥接方案 — PC-INTEGRATION

> 版本：v1.2（2026-08-06，R1/R4 裁决对齐 + Phase A 实现清单）
> 作者：be-pc（后端工程师）
> 状态：**TRTC 选型已定稿（架构师 2026-08-06）**；R1/R4 已裁决（team-lead 2026-08-06）；实现分 Phase A（哑对端互通）/ Phase B（全链路）
> 依据：docs/specs/mobile-voice-spec.md（§8.2 apm_bridge / §7 帧协议）、docs/decisions/OPEN-DECISIONS.md（O-015 全双工唯一终点）、docs/rtc-rebuild/MOBILE-INTEGRATION.md（手机端，fe-mobile，LiteAVSDK_TRTC:13.4）、docs/rtc-rebuild/QA-PLAN.md（qa）
> 契约：POST /api/v1/voice/session（手机端 KWS 唤醒后调用，fe-mobile 已确认，见 §2.3）
> 目标：删除自研 WS 中继链路（backend/relay/relay_client.py）后，电脑端改为「TRTC sidecar 对端（trtc-electron-sdk）+ Python rtc_bridge（apm_bridge 复用）」，与手机端 TRTC SDK 对端互通，桥接 MiniCPM-o Realtime API。

---

## 0. TL;DR（30 秒结论）

- **选定接入方式（架构师定稿）**：**TRTC**。TRTC **无**官方 Python 媒体 SDK（`tencentcloud-sdk-python-trtc` 仅 REST 控制面，拉不到原始音频帧）→ 电脑端对端 = **trtc-electron-sdk 隐藏 Electron sidecar 进程**（进房收/发音频）+ **Python rtc_bridge 进程**（复用 apm_bridge，经 localhost WS 桥接音频），沿用 relay_client 的"本地桥"骨架（只留 gateway 侧，去掉 relay 侧）。
- **会话契约**：手机 KWS 唤醒 → `POST <云函数>/api/v1/voice/session` → **云函数代签** `room_id + user_id(device_id) + user_sig + sdk_app_id`（§2.3 / ARCHITECTURE §3.4）；PC 轮询会话意图取自身 userSig 进同房。userSig 短时效（≤600s）、房间号规则 TRTC_ROOM_PREFIX+device_id、同 device 幂等复用房间（§2.3）。
- **音频流格式**：全链路 **16k 单声道 s16**（sidecar 用 Web Audio / SDK 音频回调定 16k）→ 与 MiniCPM-o 上行/下行 **16k s16 全链路对齐，happy path 零重采样**；仅当 sidecar 强制给 48k 时才在 rtc_bridge 做 3:1 线性抽取（§3 备选）。
- **服务架构**：**独立进程**（sidecar + rtc_bridge，与 backend(FastAPI) 分离），沿用 relay_client 先例 + jax-services.ps1 统一拉起；backend 只做**云函数会话意图轮询** + 状态协调（localhost），**签发在云函数**（§2.3）。
- **稳定性要点**：TRTC 云负责网络级自动重连（无自研心跳/重连风暴）；PC 端只负责**房间生命周期**（session 请求进房、对端离开退房、userSig 过期重签）与**进程级看门狗**（复用 jax-watchdog.ps1）。
- **分阶段实施**：**Phase A = 哑对端互通验证**（凭证到位即开工，不经 MiniCPM-o，顺带过 R1 gate）；**Phase B = 全链路**（rtc_bridge + apm_bridge + MiniCPM-o）。Phase A 清单见 §7。
- **打断语义（对齐 fe-mobile）**：手机 barge-in 靠 TRTC 播放/远端音频状态，**PC 端不做「暂停/恢复上行」逻辑**——上行音频持续推给 apm_bridge，MiniCPM-o 原生 barge-in 覆盖打断（§3.4）。

---

## 1. 背景与现状

### 1.1 当前链路（待删除）

```
手机(App: AudioRecord 16k) ──WS 二进制帧──▶ 云端中继 relay_server(CloudRun) ──WSS──▶ PC relay_client.py
                                                                                          │
                                                                              (localhost WS /ws/voice)
                                                                                          ▼
                                                                       backend voice 网关 session.py(path=apm)
                                                                                          │
                                                                              ApmBridge(feed_pcm/on_audio_out)
                                                                                          │
                                                                          MiniCPM-o Realtime API(wss)
```

痛点（团队共识，见 QA-PLAN）：自研配对状态机 / 心跳 / 重连风暴是 bug 重灾区，中继"纯透传不解析"反而把链路复杂度全部留在两端。

### 1.2 可复用资产（保留）

| 资产 | 位置 | 复用方式 |
|---|---|---|
| `ApmBridge`（全双工桥接类） | `backend/app/voice/apm_bridge.py` | **原样复用**：`feed_pcm(16k s16)` 上行 / `on_audio_out(16k s16)` 下行 / 懒初始化 / 断线重连 / 停顿补静音入口 |
| `_feed_apm_with_end_detect`（停顿补静音） | `backend/app/voice/session.py` | **抽到 apm_bridge.py 或独立 helper**，rtc_bridge 与旧 WS 路径共用（RTC 全双工同样需要"说完判定"，见 §3.4） |
| `f32_to_s16_16k`（24k f32 → 16k s16） | `apm_bridge.py` | 保留（下行 API delta 用）；sidecar 若给 48k 需新增 `s16_48k_to_s16_16k` |
| relay_client 的"本地桥"骨架 | `backend/relay/relay_client.py` | **改造复用**：去掉 relay 侧，只留 localhost WS 桥接（sidecar ↔ rtc_bridge），删 E2EE |
| 294 个单测 | `backend/tests/unit/` | 保留；新增 rtc_bridge / session 接口单测（mock sidecar 层） |
| 服务编排 | `scripts/jax-services.ps1` / `jax-watchdog.ps1` | 新增 `rtc-bridge` 服务项（sidecar 由 bridge 拉起或并列），沿用 PID 文件 + 幂等 + 看门狗模式 |

### 1.3 目标链路（TRTC 定稿）

```
手机(App: TRTC SDK 采集/播放, LiteAVSDK_TRTC:13.4)
        │ POST <云函数>/api/v1/voice/session → 拿 room_id/user_sig
        ▼ enterRoom(room_id, user_id, user_sig)
   TRTC 实时音频云 ◀──▶ PC sidecar(trtc-electron-sdk 隐藏 Electron, 进同一房间)
                              │  localhost WS（音频帧 + 控制）
                              ▼
                       PC rtc_bridge.py(Python 常驻)
                              │  (同进程)
                         ApmBridge ──▶ MiniCPM-o Realtime API(wss)
```

- 手机 TRTC SDK 内置 AEC/采集/播放/弱网重连（fe-mobile 文档 §1，AEC 免费能力）。
- sidecar 是**无头对端**（headless peer）：不开扬声器外放（避免 PC 侧回声），只订阅手机音频帧 + 推送 MiniCPM-o 回复音频；不开本地麦克风（上行只走 rtc_bridge 注入的 MiniCPM-o 下行音频，不采集本机声音）。
- 手机端 mic handoff（fe-mobile）：监听阶段 MicRecorder 常驻 KWS → 唤醒后停 MicRecorder 释放 mic → TRTC SDK 进房自行采集/播放 → 通话结束 exitRoom 重启 MicRecorder。会话期 barge-in 不依赖本地 mic VAD，靠 TRTC 播放/远端音频状态。

---

## 2. RTC 对端接入（TRTC 定稿）

### 2.1 选型结论（架构师定稿）

| 维度 | 结论 |
|---|---|
| 厂商 | **TRTC（腾讯）**（架构师 2026-08-06 定稿；与项目腾讯云/CloudBase 生态一致） |
| 手机端 | `LiteAVSDK_TRTC:13.4`（fe-mobile） |
| 电脑端对端 | **`trtc-electron-sdk`（npm，13.3.801 为最新发布，跟踪原生 13.3 线；13.4 线发布后升到对应 13.4.x）** —— TRTC 无 Python 媒体 SDK，Electron sidecar 是唯一 PC 对端路径 |
| 后端签发 | **云函数代签（方案 A，架构师/team-lead 2026-08-06 裁决，ARCHITECTURE §3.4）**：`GenUserSig`（HMAC-SHA256，SDKAppID + SecretKey，纯 Python 可生成）放入 CloudBase/SCF 云函数 `trtc-sign`；SecretKey 唯一存云函数环境变量，PC .env 生产路径置空（Phase A 本地冒烟可用 .env 临时签发） |
| 媒体加密 | `enablePayloadPrivateEncryption`（**付费能力，需开通**；MVP 若不付费，依赖 TRTC 传输层 TLS 加密，风险记录见 §6 R4） |
| 免费额度 | 新账号约 1 万分钟/月（首月，以控制台为准，架构师核算） |

### 2.2 PC 对端形态：sidecar + rtc_bridge 双进程

```
┌────────────────────────── Windows PC ─────────────────────────────────┐
│                                                                       │
│  sidecar(trtc-electron-sdk, 隐藏窗口)         rtc_bridge.py(Python)   │
│  ├ TRTCCloud 实例（进房/订阅/发布/回调）        ├ 本地 WS 服务端(:19092) │
│  ├ 收手机音频 → 转 16k s16 → localhost WS ───▶ ├ up_q → ApmBridge      │
│  ├ localhost WS ◀── 16k s16 下行 ────────────── ├ down_q ← on_audio_out│
│  └ (不开扬声器/不开本地麦克风)                   └──▶ MiniCPM-o API      │
│                                                                       │
│  backend(FastAPI :8000) ——localhost 控制 WS/HTTP──▶ rtc_bridge        │
│   ├ POST /api/v1/voice/session（签发凭证+拉 sidecar 进房）              │
│   └ GET  /api/v1/voice/status（透传 rtc_bridge 状态）                   │
└───────────────────────────────────────────────────────────────────────┘
```

- **sidecar**（Electron + trtc-electron-sdk）：职责最小化 = 进房 + 音频双向桥接 + 状态上报；无 UI（隐藏窗口，最小窗口或 tray 常驻）。**版本锁定**：`package.json` 写精确版本 `"trtc-electron-sdk": "13.3.801"`（13.4 线发布后升对应 13.4.x），**禁 `latest`**；Electron ≥ 8.5.0（推荐 ≥ 22 LTS 线），Node.js ≥ 16.20.2；写入 ADR（见 §8）。
- **rtc_bridge**（Python）：常驻进程，持有 ApmBridge；通过 localhost WS 与 sidecar 双向桥音频（sidecar 是 WS 客户端，rtc_bridge 是 WS 服务端，绑定 127.0.0.1:19092 不对外）。
- **backend 与 rtc_bridge**：**签发在云函数**（§2.3），backend 只做**会话意图轮询**（`GET <云函数>/pending` → `POST <云函数>/sign` 取 PC userSig）→ 经 localhost 控制通道通知 rtc_bridge → rtc_bridge 经 localhost WS 通知 sidecar `enterRoom`。三者都在本机，控制面量小；backend 与云函数之间为**出站 HTTPS**（PC 无入站）。

### 2.3 会话契约（已确认，实现对齐）

`POST /api/v1/voice/session`（手机 KWS 唤醒后调用；fe-mobile 已确认按此调用）

```jsonc
// 请求（发给云函数 trtc-sign，非 PC 后端）
{ "device_id": "jax-xxxxxxxx" }   // pairing_code 已废弃语义，MVP 可省略
// 响应 200（wire 层 snake_case；手机 Kotlin 映射 camelCase）
{
  "room_id": "jax-jax-xxxxxxxx",  // room_id = TRTC_ROOM_PREFIX + device_id（规则定稿 §3.4，非随机）
  "user_id": "jax-xxxxxxxx",      // 手机进房 userId = device_id（与 sidecar "jax-pc-sidecar" 区分）
  "user_sig": "<云函数 GenUserSig 签发>",  // 短有效期 ≤600s
  "sdk_app_id": 1600155678,       // int
  "scene": "audio_call"
}
```

- **语义**：手机 KWS 唤醒后调用**云函数**（`trtc-sign`，HTTP 触发器公网可达）→ 云函数 ①校验 device_id 白名单 ②计算 room_id = `TRTC_ROOM_PREFIX + device_id` ③签发手机 userSig（userId=device_id，expire ≤600s）④写"会话意图"记录（device_id/room_id/ts，供 PC 轮询）⑤返回凭证 → 手机 `enterRoom(room_id, user_id=device_id, user_sig, scene="audio_call")`。
- **PC 进房协调（PC 无公网入站）**：PC 后端常驻轮询器每 ~2s `GET <云函数>/api/v1/voice/session/pending?device_id=`；发现未消费会话意图 → `POST <云函数>/api/v1/voice/session/sign`（body `{device_id, user_id:"jax-pc-sidecar"}`）取 PC 自身 userSig → 通知 sidecar 进同一 room_id → 标记意图已消费。手机可先入房等待，PC 通常 ≤2s 加入。
- **鉴权**：MVP 单用户，`device_id` 白名单 + 可选 `X-Device-Token`（后续可升级正式态 device 注册/绑定）。**SecretKey 唯一存云函数环境变量**（`TRTC_SECRETKEY`），PC .env 生产路径置空（Phase A 本地冒烟例外），不落 repo、不进日志。
- **userSig 有效期 ≤10min**：`GenUserSig` 的 `expire` 参数传 ≤600s；过期后手机重进需重新调 session 接口。
- **房间号规则（定稿，ARCHITECTURE §3.4）**：`room_id = TRTC_ROOM_PREFIX + device_id`（`TRTC_ROOM_PREFIX` 环境变量 = `jax-`）；确定性、单用户隔离足够，防枚举依赖 userSig 房间鉴权（无合法 userSig 无法进房），而非房间号不可猜。同 `device_id` 幂等**复用房间**（会话期内重复请求返回同一 room_id，不重复拉 sidecar 进房）；会话结束（手机退房/超时）后释放。
- **错误语义**：后端/SDK 不可用 → 非 200 + `{code, message}`（沿用统一响应格式 `{code, data, message}` 包装，见 §4.4）。

### 2.4 版本锁定（写进 ADR，禁 latest）

| 依赖 | 锁定要求 |
|---|---|
| `trtc-electron-sdk`（sidecar） | 精确版本 `13.3.801`（现最新，对应原生 TRTC SDK 13.3 线；**13.4 线发布后升到对应 13.4.x**）；`package.json` 写死，不用 `^` 或 `latest`；npm lockfile 提交 |
| `electron`（sidecar 运行时） | 精确版本（≥ 8.5.0 兼容；推荐 ≥ 22 LTS 线），同样写死 |
| `LiteAVSDK_TRTC`（手机端） | `13.4`（fe-mobile 锁定） |
| 后端 `GenUserSig` 实现 | 纯 Python（HMAC-SHA256），不依赖腾讯云 SDK 大包，锁版本于 `requirements.txt`（如 `pyjwt`/自实现，不引 `tencentcloud-sdk-python-trtc` 全量包——只用于 REST 控制面，本项目不需要） |

> 版本对应关系：npm `trtc-electron-sdk` 版本号直接跟踪原生 SDK（13.3.x）；手机 13.4 与 sidecar 13.3.801 属同一大版本线，互通无碍（TRTC 房间互通跨小版本）。升级 sidecar 到 13.4.x 待 npm 发布后同步，写入 ADR 更新记录。

---

## 3. 音频流转设计（sidecar ↔ MiniCPM-o）

### 3.1 格式全景（全链路 16k s16，happy path 零重采样）

```
手机 TRTC SDK 采集(内部 48k) ──▶ TRTC 云(编码/弱网/转发) ──▶ PC sidecar
                                                               │ TRTC 远端音频回调 / Web Audio
                                                               ▼
                                                          (重采样到 16k mono s16 —— 若 SDK 直接给 16k 则零转换)
                                                               │ localhost WS
                                                               ▼
                                                          rtc_bridge: ApmBridge.feed_pcm(16k s16)
                                                               │ 累积 1s(32000B) → f32 base64 → input.append
                                                               ▼
                                                          MiniCPM-o API(16k 上行) → (24k f32 下行)
                                                               │ f32_to_s16_16k(24k→16k 3:2 线性抽取)
                                                               ▼
                                                          ApmBridge.on_audio_out(16k s16)
                                                               │ 下行整形器(拆分/节拍,见 §3.3)
                                                               ▼ localhost WS
                                                          sidecar: TRTC 推流(16k s16 注入) → TRTC 云 → 手机播放
```

**关键结论**：上行 `sidecar帧 → apm_bridge` 与下行 `apm_bridge → sidecar` 两侧**都是 16k s16**，中间夹着 apm_bridge 内部 1s 块累积和 24k→16k 下行重采样（已有函数），**没有额外重采样点**。与现有手机 WS 协议的 `pcm_s16le_16k` 完全一致，音频管线可原样迁移。

### 3.2 上行（手机 → sidecar → rtc_bridge → API）

- **sidecar 收流**（二选一，实现时定）：
  - TRTC Electron SDK 音频回调：`TRTCCloud.on('onRemoteUserAudioFrame')` / 音频帧回调（需确认 Electron SDK 是否暴露原始 PCM 帧；若只暴露 volume 事件，则走 Web Audio 方案）；
  - **Web Audio 方案（兜底）**：sidecar 用 `<audio>`/`AudioContext` 拉远端音频流 → `AudioContext({sampleRate:16000})` 或 `ScriptProcessor/AudioWorklet` 拿 16k mono Float32 → 转 s16 → 送 WS。**采样率转换点在 sidecar**（48k→16k，3:1 抽取，参考 `f32_to_s16_16k` 的 numpy 索引写法）。
- **回调/worker 内只拷贝**：sidecar 把 16k s16 帧 `ws.send(bytes)`；rtc_bridge 侧放入 `asyncio.Queue`（不阻塞 WS 回调），由独立消费协程调 `await bridge.feed_pcm(s16_bytes)`。
- 帧尺寸：sidecar 按 10/20/40ms 来帧，`feed_pcm` 内部按 1s 累积，任意帧长都兼容。
- **不开本地麦克风/扬声器**：sidecar 只订阅远端 + 注入外部音频源，避免本机回声/啸叫（TRTC Electron `startLocalAudio` 不调用；下行用 `enableCustomAudioCapture`/音频注入 API 推外部 PCM）。

### 3.3 下行（API → rtc_bridge → sidecar → 手机）

- `ApmBridge.on_audio_out(pcm_s16)` 回调（16k s16，块大小随 API delta 变化）→ 下行队列。
- **下行整形器（DownlinkShaper）**：把 API 变长块按 sidecar 期望帧长（如 10ms=160 样本）**拆分 + 节拍推送**；节拍用"消费时长 = len/32000 秒"的 `asyncio.sleep` 对齐（参考官方示例 pacer 思路），避免一次性灌入导致手机端卡顿/爆音。
- **打断（barge-in）——对齐 fe-mobile，PC 端不做特殊协议**：
  - 手机侧：会话期 barge-in 靠 **TRTC 播放/远端音频状态**（用户开口 → TRTC SDK 检测 → 停播放），fe-mobile 负责；
  - PC 侧：**上行持续推给 apm_bridge**（不做「暂停/恢复上行」逻辑），MiniCPM-o 原生 `force_listen=false` barge-in 自动打断生成；下行整形器**不依赖上行 VAD 做 flush 语义**——模型自己停，队列自然清空（可选优化：检测到 `speech_start` 时丢弃已入队未推的尾部音频以降低感知延迟，**仅作延迟优化、不作打断协议**，且需与 fe-mobile 确认不冲突）。
  - ⚠️ **双端语义不打架**：PC 端不因"手机在说话"而停上行（那会打断 MiniCPM-o 的听），也不因本地检测去做"暂停/恢复"状态机。

### 3.4 说完判定 / 停顿补静音（必须保留）

- 现 `session.py::_feed_apm_with_end_detect`（低能量 >1.2s → 补 2s 纯静音触发模型说完判定；能量回升 → 重置）**在 RTC 全双工下同样必需**：手机 TRTC SDK 采集会持续送帧（含底噪），模型 VAD 判定不了"你说完"→ 永不回复（2026-08-06 现场实锤）。
- 落地：把该函数从 `session.py` **抽取为 apm_bridge 的辅助函数/独立模块**（`app/voice/end_detect.py` 或并入 apm_bridge.py），rtc_bridge 上行消费协程直接调用。
- 该"补静音"逻辑**不是**打断协议（不打断上行），只是触发模型"说完"判定，与 fe-mobile 的播放态 barge-in 不冲突。

### 3.5 备选：若 sidecar 强制 48k

- 在 sidecar 或 rtc_bridge 加一个**重采样点**（收流后、feed 前）：48k s16 → 16k s16 线性抽取 3:1（参考 `f32_to_s16_16k` 的 numpy 索引写法，新增 `s16_48k_to_s16_16k`）。
- 下行同理：若 sidecar 要求 48k 注入，则 16k → 48k 线性插值 1:3 后推送；**优先争取 16k 直通**（Web Audio 方案可直接定 16k）。

---

## 4. 服务架构（删除 relay_client 后）

### 4.1 进程拓扑（TRTC 定稿版）

```
┌──────────── Windows PC ────────────────────────────────────────────────┐
│                                                                       │
│  backend(FastAPI :8000)            rtc_bridge.py(独立进程)              │
│  ├ brain/监控/推送(不变)              ├ localhost WS 服务端(:19092)        │
│  ├ 轮询云函数 pending/sign(出站HTTPS) │ ├ PeerSession×N(每设备一房间)       │
│  │  → 通知进房(room_id/user_sig)      │  ├ ApmBridge×N(复用原类)          │
│  ├ GET  /api/v1/voice/status ◀───────│  └ 队列: up_q / down_q           │
│  └(不再挂 /ws/voice 手机直连)          └──▶ MiniCPM-o Realtime API        │
│                                                                       │
│  sidecar(Electron, 隐藏窗口) ──localhost WS──▶ rtc_bridge              │
│  └ trtc-electron-sdk 进房(room_id)                                    │
│      手机(App TRTC SDK) ──▶ TRTC 云 ◀── sidecar(同一房间)               │
└───────────────────────────────────────────────────────────────────────┘
```

### 4.2 独立进程（推荐） vs 同进程

| 维度 | **独立进程 rtc_bridge.py（推荐）** | 同进程（挂在 FastAPI lifespan） |
|---|---|---|
| 崩溃隔离 | RTC 通话挂了不影响监控/大脑主服务 | rtc_bridge 崩溃 = 整个 backend 重启 |
| 重启影响 | backend 升级重启不打断进行中的 RTC 通话（bridge 独立续跑） | backend 一重启，所有通话断 |
| sidecar 耦合 | sidecar 只连 rtc_bridge，与 backend 解耦 | sidecar 需连 backend 进程，耦合加深 |
| 编排 | 沿用 relay_client 先例（jax-services.ps1 加 `rtc-bridge` 项） | 需要改 uvicorn 启动流程，侵入 main.py |

**结论**：独立进程。ApmBridge 类从 backend 包**原样 import 复用**（`from app.voice.apm_bridge import ApmBridge`），不随 backend 进程跑。控制面通信极轻（session 签发/状态/健康），不承载音频（音频只在 sidecar ↔ rtc_bridge 之间 localhost WS）。

### 4.3 模块划分（落地时参考，单文件 ≤300 行）

```
backend/rtc_bridge/            # Python 侧（复用 app.voice.apm_bridge）
├── __init__.py
├── main.py                    # 入口：加载配置 → 起 localhost WS 服务端 → 常驻事件循环（只装配）
├── config.py                  # TRTC SDKAppID/SecretKey(仅.env)/房间规则 + sidecar WS 端口
├── bridge_server.py           # localhost WS 服务端（收 sidecar 上行帧 → up_q；down_q → 下发）
├── session.py                 # PeerVoiceSession：1 房间 = 1 WS 连接 + 1 ApmBridge + 状态机
├── manager.py                 # 多会话注册/互斥/清理（对齐 VoiceSessionManager 语义）
├── control.py                 # 后端控制通道（join/leave/status 指令接收）
└── shaper.py                  # 下行整形器（拆帧 + 节拍；可选延迟优化 flush）

sidecar/                       # Electron 侧（trtc-electron-sdk）
├── package.json               # trtc-electron-sdk 精确版本（禁 latest）+ lockfile 提交
├── main.js                    # 隐藏窗口 + 生命周期
├── rtc.js                     # TRTCCloud 进房/订阅/外部音频注入/回调
└── bridge.js                  # localhost WS 客户端：音频帧 + 控制
```

### 4.4 backend 侧改动（最小化）

- `routes_voice.py` 新增 `POST /api/v1/voice/session`（§2.3 契约）：
  - 校验 device_id → 计算/复用 `room_id`（TRTC_ROOM_PREFIX+device_id）→ `GenUserSig(sdk_app_id, secret_key, device_id, expire≤600s)`（生产在云函数执行）→ 写会话意图 → 返回 `{room_id, user_id, user_sig, sdk_app_id, scene}`；PC 侧轮询意图取自身 userSig；
  - 统一响应包装 `{code:0, data:{...}, message:""}`；错误用 `{code, message}`（沿用项目统一格式）。
- `GET /api/v1/voice/status` 增加 rtc_bridge / sidecar 状态透传（进程存活 / 房间数 / 连接状态 / sidecar SDK 版本）。
- `main.py`：不再装配手机 WS 直连（`/ws/voice` 是否保留由架构师删除清单定稿；若保留仅供 LAN 直连调试，`path=apm` 分支可留）。
- `session.py`：`_feed_apm_with_end_detect` 抽出共享；apm 装配逻辑移交给 rtc_bridge。
- `.env` 新增：`TRTC_SDKAPPID` / `TRTC_SECRETKEY`（**仅 Phase A 本地冒烟**；Phase B 起生产唯一持有方为云函数环境变量，PC .env 置空）/ `TRTC_ROOM_PREFIX`（=`jax-`）；`RTC_BRIDGE_WS_PORT=19092`。`.env.example` 同步补 TRTC 模板（占位，不落真实值）。

---

## 5. 稳定性与运维要点（TRTC SDK 接管重连后）

### 5.1 分层职责（谁管什么）

| 层 | 谁负责 | 说明 |
|---|---|---|
| 网络级自动重连 | **TRTC SDK / 云** | 弱网/切网/SDK 内部自动恢复，**不写自研心跳**（删除 relay 的心跳/假死感知/指数退避那一整套） |
| 房间生命周期 | **rtc_bridge + sidecar** | 进房/退房/对端离开/异常房间清理（本节剩余部分） |
| 进程级存活 | **jax-watchdog.ps1 / jax-services.ps1** | sidecar / rtc_bridge 崩溃或健康检查失败 → 拉起；复用现看门狗 |
| API 会话 | **ApmBridge（已有）** | 懒初始化 + 断线重连 + 600s 空闲超时（保留不动） |

### 5.2 房间生命周期

- **进房**：`POST /api/v1/voice/session` → rtc_bridge 通知 sidecar `enterRoom(room_id, user_id="jax-pc-sidecar", user_sig)` → sidecar 回调 `onEnterRoom` 成功 → 创建/复用 `ApmBridge`（**不主动 start**，保持懒初始化：首个音频块才建 API 会话——沿用 2026-08-06 实锤教训，空闲连接会被 API 服务端回收）→ 回报后端"已就绪" → 手机可进房。
- **对端（手机）离开**：`onRemoteUserLeave` / 远端音频停止 → ① `ApmBridge.close()`（释放 API 会话）② 清 up_q/down_q ③ sidecar `exitRoom()` ④ 回报后端状态 → rtc_bridge 回待命（不退出进程，等下次 session 请求）。
- **手机重进同一房间**：SDK 自动重订阅；rtc_bridge 需在远端用户重新加入时**重置说完判定状态**（`_last_voice_ts`/`_silence_padded`）与下行整形器，防止跨会话状态污染（对齐 relay_client 里"新会话重置防重放"的教训）。
- **异常房间清理**：
  - TRTC 房间末位用户退房自动销毁（云侧），我们**无需自建房间清理服务**；
  - 但 rtc_bridge 要处理"进房后长时间无对端"（如 120s 无远端用户加入）→ 通知 sidecar 退房回待命 + 告警，防僵尸 connection 堆积（对齐 relay 假死感知的教训：连上但无事发生 ≠ 健康）。
  - userSig 过期：手机重进需重新调 `/api/v1/voice/session`；sidecar 侧用 `onUserSigExpired` 回调 → 向 backend 请求新 userSig → 重进房（指数退避 1s→2s→4s…≤30s，复用现 RECONNECT_BACKOFF 常量）。

### 5.3 健康检查与可观测（复用现基础设施）

- rtc_bridge 暴露 `GET 127.0.0.1:19093/health` + `/metrics`（bridge 自带 HTTP 或经 backend 代理）：
  - 健康：进程存活 + WS 服务端已监听 + sidecar 已连接（≥0 个活动房间，空闲也算健康，避免看门狗误杀待命态）。
  - 指标：`rooms`, `sidecar_connected`, `trtc_conn_state`, `apm_session_state`, `up_frames`, `down_frames`, `last_peer_ts`, `reconnects`, `sidecar_sdk_version`。
- sidecar 上报：SDK 版本 / 进房状态 / 远端用户状态，经 localhost WS 送 rtc_bridge 聚合。
- `jax-services.ps1` 加 `rtc-bridge` 服务项（health 判定用 `/health`，不等"配对成功"——待命即健康）；`jax-watchdog.ps1` 若检测 `/health` 失败 → 拉起（sidecar 由 rtc_bridge 拉起或 watchdog 双拉起）。
- 日志：`logs/rtc_bridge.log` / `logs/sidecar.log`；房间进出/重连/APM 会话状态打点（对齐 relay_client 日志风格）。
- 状态透传：backend `/api/v1/voice/status` 轮询 rtc_bridge `/metrics`，App/桌宠可见"对端在线/离线/通话中"。

### 5.4 删除清单（落地时）

| 删除 | 说明 |
|---|---|
| `backend/relay/relay_server.py` | 云端中继（CloudRun 服务 jax-relay 一并下线，见 OPS-002） |
| `backend/relay/relay_client.py` | PC 桥接，由 sidecar + rtc_bridge 取代（"本地桥"骨架可改造复用） |
| `backend/relay/relay_protocol.py`（E2EE 部分） | E2EE 已废弃（ADR 记录，team-lead 裁决 2026-08-06）：MVP 不开通 TRTC 付费私有加密，用 TRTC 默认传输加密（TLS）承担媒体链路加密；`load_e2ee_key` 若仍被 voice/config.py 引用需清理 |
| `backend/app/voice/e2ee.py` + voice/config.py 的 e2ee 装配 | 应用层 E2EE 废弃（RTC 传输层加密替代，对齐 ARCHITECTURE §4.1）；`load_voice` 的 e2ee 参数清理 |
| routes_voice.py 的 `/ws/voice`、`/api/v1/voice/stream`、`/api/v1/voice/pair` 端点 | 手机 WS 直连/局域网直连/配对统一走 TRTC（ADR-012 #4）；保留 status，新增 session |
| .env 的 `RELAY_TOKEN` / `RELAY_E2EE_KEY` / `VOICE_TOKEN` | relay 残留清理（对齐 ARCHITECTURE §4.1） |
| 手机 `VoiceWsClient` / 配对状态机 / FrameCodec / VoiceCipher | fe-mobile 文档 §0 已列 |
| relay 相关单测 | `test_relay_*.py` 由 rtc_bridge / session 接口单测取代；294 个其余单测保留 |

---

## 6. 风险与待裁决项

| # | 项 | 风险/说明 | 建议 |
|---|---|---|---|
| R1 | **TRTC Electron SDK 原始音频帧获取方式** | 需确认 `trtc-electron-sdk` 是否直接暴露远端 PCM 帧；若无，用 Web Audio/AudioWorklet 兜底（16k 可定） | **✅ 已裁决：Phase B gate（不阻塞 Phase A）**。Phase A 哑对端互通验证时同时冒烟确认 PCM 帧回调（进房→收帧→推帧→回环）；拿不到原始帧 → 启用 Web Audio 兜底，音频管线不变（仍 16k s16）。判定入口见 §7 A4 |
| R2 | **Electron sidecar 引入 Node 运行时** | PC 新增 Electron/Node 依赖；开机自启/隐藏窗口/崩溃拉起需纳入 jax-services | 接受（TRTC 唯一路径）；sidecar 尽量精简（无 UI、单实例） |
| R3 | **userSig/SecretKey 安全** | SecretKey 若泄露可被冒充进房 | SecretKey 唯一存云函数环境变量（PC 生产置空，对齐 ARCHITECTURE §3.4）；userSig ≤10min + device_id 白名单鉴权；后续 device 绑定升级 |
| R4 | **TRTC 媒体私有加密为付费能力** | `enablePayloadPrivateEncryption` 需开通；不付费则依赖 TRTC 传输层 TLS | **✅ 已裁决（team-lead 2026-08-06）：MVP 不开通**。用 TRTC 默认传输加密（TLS），家庭/个人场景信任边界足够；E2EE 废弃已记 ADR。后续需强加密再评估付费能力——记入 ADR 后果，不阻塞 |
| R5 | **说完判定依赖能量阈值** | 停顿补静音逻辑迁移后需在 RTC 路径回归（QA G3） | 抽公共模块 + 单测覆盖（静音/说话/打断三态） |
| R6 | **打断语义双端一致性** | 手机靠 TRTC 播放态 barge-in，PC 端不做暂停/恢复上行 | 已对齐（§3.3）；fe-mobile 与 be-pc 各持一端，联调时以 QA 打断用例（G1 <500ms）验证 |
| R7 | **免费额度** | TRTC 约 1 万分钟/月；常驻监听会持续占通话时长 | 架构师核算；"唤醒后才进房"（mic handoff）天然省时长 |

---

## 7. Phase A 实现清单（哑对端互通验证）

> 目标：**凭证（SDKAppID/SecretKey）到位后最先开工**。用「手机 RtcClient + PC 哑对端」验证 手机↔sidecar 互通（**不经 MiniCPM-o / apm_bridge**），同时冒烟确认 sidecar PCM 帧回调（**R1 gate**）。Phase A 通过后再进 Phase B（rtc_bridge + apm_bridge 全链路）。
> 负责人：be-pc（PC 哑对端 + 后端 session 接口）+ fe-mobile（手机 RtcClient）。

### 7.1 前置准备（凭证到位前即可做，不阻塞）

| # | 项 | 说明 |
|---|---|---|
| P1 | `sidecar/` 脚手架 | Electron + `trtc-electron-sdk@13.3.801`（精确版本 + lockfile 提交，禁 latest）；隐藏窗口 + 生命周期骨架 |
| P2 | `backend/rtc_bridge/` 骨架 | 目录/配置/单测框架；`GenUserSig` 纯 Python 函数 + 单测（附录 A.1） |
| P3 | session 接口（mock 态） | `POST /api/v1/voice/session` 骨架：无凭证时返回明确错误码（如 `config_missing`），凭证到位即切实签 |
| P4 | 哑对端测试工具 | 下行推测试音（1kHz/语音 wav → 16k s16）脚本；上行收帧打印帧计数/能量脚本 |
| P5 | `.env` 模板 | `TRTC_SDKAPPID` / `TRTC_SECRETKEY` / `TRTC_ROOM_PREFIX` 占位（gitignore，不落 repo；SecretKey 生产唯一存云函数环境变量） |
| P6 | 手机端 RtcClient 骨架 | fe-mobile：`RtcClient.kt` + mic handoff 骨架（其文档 §0） |

### 7.2 凭证到位后执行（Phase A 主线）

| # | 步骤 | 通过标准 |
|---|---|---|
| A1 | 云函数 `POST /api/v1/voice/session` 实签 | 返回 `{room_id=TRTC_ROOM_PREFIX+device_id, user_id=device_id, user_sig, sdk_app_id, scene:"audio_call"}`；同 device 重复请求返回同一 room_id（幂等）；userSig 解析 expire ≤600s |
| A2 | sidecar 进房 | `onEnterRoom` 成功回调；`getSDKVersion()` = 13.3.801 |
| A3 | 手机 RtcClient 进房（fe-mobile） | 与 sidecar 同一房间；`onRemoteUserEnter` 双方互见 |
| A4 | **R1 gate：上行收帧冒烟** | 手机说话 → sidecar 收到 16k s16 帧（帧计数/能量持续增长）。**判定**：拿到原始 PCM 帧 → Phase B 按原设计；拿不到 → 启用 Web Audio/AudioWorklet 兜底（16k 可定），音频管线不变 |
| A5 | 下行推流验证 | sidecar 推测试音（16k s16）→ 手机听到（TRTC 播放态正常） |
| A6 | 对端离开 / 房间清理 | 手机退房 → sidecar `onRemoteUserLeave` → exitRoom → 回待命；手机重进 → 复用同一房间（状态重置） |
| A7 | 打断语义预检（可选） | 确认上行持续（PC 侧无暂停/恢复逻辑），为 Phase B barge-in 打底 |

### 7.3 Phase A 交付物与验收

- **交付物**：① `sidecar/` 哑对端（可独立跑通回环）② `backend/rtc_bridge/` 骨架 + `GenUserSig` + session 接口（mock→实签）③ 联调报告（A1–A7 结果 + **R1 gate 结论**）。
- **验收**：QA-PLAN 冒烟门前置版——手机进房 → 说话 →（哑对端回环/测试音）→ 听到 → 退房，30 分钟内跑通。**R1 gate 结论记录为 Phase B 是否需 Web Audio 兜底的唯一依据**。
- **不阻塞项**：ApmBridge / MiniCPM-o / 停顿补静音 / 下行整形器均属 Phase B，Phase A 不涉及。

---

## 8. 迁移步骤（建议顺序，配合架构师 §5）

1. **写 ADR**：TRTC 选型 + sidecar 版本锁定（`trtc-electron-sdk` 13.3.801 / 13.4.x 线，Electron ≥8.5.0 推荐 ≥22 LTS，禁 latest）+ 会话契约定稿。
2. 后端/云函数：新增云函数 `POST /api/v1/voice/session`（代签 + 会话意图）+ PC 意图轮询 + `.env`（`TRTC_SDKAPPID`/`TRTC_SECRETKEY`/`TRTC_ROOM_PREFIX`）+ `voice/status` 透传。
3. 新建 `sidecar/`：先跑通"进房 → 收手机音频 → WS 送 rtc_bridge → 下行推回"最小闭环（TRTC Electron Demo 基础上改）。
4. 新建 `backend/rtc_bridge/`：localhost WS 服务端 + ApmBridge 装配 + `_feed_apm_with_end_detect` 抽公共模块 + 单测（mock sidecar 层）。
5. `jax-services.ps1` / `jax-watchdog.ps1` 加 `rtc-bridge`（及 sidecar 拉起）项；删除 relay 三件套 + CloudRun 服务下线。
6. 与 fe-mobile 联调（真机跨网，验证会话契约 + barge-in 双端一致性）→ 按 QA-PLAN 六道门验收。

---

## 附录 A：关键 API / 实现参考

### A.1 GenUserSig（后端签发，纯 Python）

```python
# 参考腾讯云 TRTC GenUserSig 算法（HMAC-SHA256），不依赖腾讯云 SDK 大包
import base64, hashlib, hmac, json, time

def gen_user_sig(sdk_app_id: int, secret_key: str, user_id: str, expire_s: int = 600) -> str:
    payload = {"TLS.ver": "2.0", "TLS.identifier": user_id,
               "TLS.sdkappid": sdk_app_id, "TLS.expire": expire_s,
               "TLS.time": int(time.time())}
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sig = hmac.new(secret_key.encode(), plain.encode(), hashlib.sha256).digest()
    payload["TLS.sig"] = base64.b64encode(sig).decode()
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
```

### A.2 sidecar（trtc-electron-sdk）最小骨架

```js
// bridge.js —— localhost WS 客户端（连 rtc_bridge :19092）
const TRTCCloud = require('trtc-electron-sdk').default;
const cloud = TRTCCloud.getTRTCShareInstance();

cloud.on('onEnterRoom', (elapsed) => ws.send(JSON.stringify({type:'entered', elapsed})));
cloud.on('onRemoteUserLeave', (uid, reason) => ws.send(JSON.stringify({type:'peer_left', uid})));
// 远端音频帧/Web Audio 收 16k s16 → ws.send(bytes)；WS 收下行 bytes → 外部音频注入推流

function enterRoom({room_id, user_id, user_sig, sdk_app_id}) {
  const p = new TRTCParams();
  p.sdkAppId = sdk_app_id; p.userId = user_id; p.userSig = user_sig; p.roomId = room_id;
  cloud.enterRoom(p, TRTCAppScene.TRTCAppSceneAudioCall);  // 纯音频通话场景
}
```

> 参考：TRTC Electron 官方文档（`trtc.io/zh/document/35097` 导入 SDK；`/document/48049` 进房；`trtc-electron-sdk` npm 13.3.801）。后端 GenUserSig 官方参考：腾讯云 TRTC 控制台"快速跑通"/UserSig 计算。

---

## 附录 B：与现有文件对照

| 现文件 | RTC 重构后 |
|---|---|
| `backend/app/voice/apm_bridge.py` | **保留**（原样复用） |
| `backend/app/voice/session.py` | `_feed_apm_with_end_detect` 抽共享；手机 WS 直连逻辑按删除清单裁剪 |
| `backend/relay/relay_client.py` | 删除 → `backend/rtc_bridge/` + `sidecar/` 取代（本地桥骨架改造） |
| `backend/relay/relay_server.py` | 删除 + CloudRun jax-relay 下线 |
| `backend/app/api/routes_voice.py` | 新增 `POST /api/v1/voice/session`；`status` 透传 rtc_bridge |
| `scripts/jax-services.ps1` | 加 `rtc-bridge`（+sidecar 拉起）服务项 |
| `backend/tests/unit/`（294） | 保留；relay 相关单测替换为 rtc_bridge / session 接口单测 |
