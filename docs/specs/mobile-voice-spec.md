# 手机端规格 — 全双工实时语音（V1.5 主线：手机一直在听 / 随时唤醒 / 随时打断 / 语音对话）

> 版本：v1.0（V1.5 手机语音主线设计定稿）
> 日期：2026-08-03
> 状态：已确认 · 供手机 App + PC voice 网关 + 中继照做（APM 双工细节待 PoC B3 回填 §8 的 `{{POC-B3}}` 占位）
> 依据：docs/decisions/OPEN-DECISIONS.md（O-014 手机语音对话）、docs/decisions/ADR-003-voice-pipeline.md（模型原生全双工 + sherpa/edge 降级链）、docs/specs/backend-brain-spec.md（大脑 intent API）、docs/specs/backend-llama-client-spec.md（本地引擎 SSE 契约）、docs/PRD.md（§6.3 语音 V-* / §6.6-6.8 大脑 / §7.2 全双工验收）
> 关联决策：本设计为 V1.5 主线升级（替代 O-014 近期"飞书语音消息"路径；飞书降为文本推送）。唤醒词主选 sherpa-onnx KWS（与 PC 降级链同框架），升格建议见 §16 ADR。

---

## 0. TL;DR（30 秒结论）

**用户形态**：手机 = 贾克斯的"随身语音助手"——常驻后台一直在听，说"贾克斯"唤醒，直接语音问答，答到一半可打断。对标三星 Bixby / GPT-Live 体验。

**架构一句话**：`Android App（前台服务常驻麦克风 + 本地唤醒词 + 流式 WS）→ 云端 WS 中继（TLS + token，纯透传不解析）→ PC 贾克斯 voice 网关 → 双路径（A 本地模型原生全双工 / B sherpa STT → 大脑 intent API → TTS 半双工）`。

**版本归属**：手机语音全双工升级为 **V1.5 主线**（替代"飞书语音消息"，飞书降为文本推送，O-014 远期路径提前）。里程碑 M1 唤醒 → M2 流式双向 → M3 全双工打断。

**平台诚实声明**：Android 可常驻监听（前台服务）；**iOS 后台常驻麦克风受系统限制，本设计仅 Android，iOS 明确 out-of-scope**。常驻监听功耗预估 5-15%/天（见 §5.5，需 M1 实测校准）。

---

## 1. 目标与背景

### 1.1 要解决的问题

用户核心诉求是**手机跟贾克斯语音对话**（唤醒→说话→语音回答），这是主形态，文字推送只是辅助。O-014 原近期路径（飞书语音消息）是"异步语音消息"形态，体验是：打开飞书 → 按住说话 → 等待回复 → 播放语音，非实时、无打断。用户已明确要"类小度/Bixby/GPT-Live"的**实时全双工**。

### 1.2 成功长什么样

- 手机息屏放兜里，说"贾克斯"，屏幕亮起/悬浮窗提示"在听"，直接说"Codex 现在跑偏了吗"，0.5-1.5 秒内听到语音回答。
- 回答到一半想说"停，我是说另一个项目"，话音一起，回答 500ms 内停住切回听，直接继续对话。
- 不在家（4G/异地）同样可用（经云端中继）；同一 Wi-Fi 下走局域网直连更快。
- 说"帮我把这个项目的数据层拆成接口+实现" → 走现有大脑（DeepSeek 拆解）→ 语音播报拆解结果 + 桌宠确认卡。
- 全程录音即发即弃，不落盘、不进日志、中继不解析内容（可选 E2EE）。

### 1.3 三条硬边界（延续 O-006/O-013）

1. **录音不出必要范围**：音频仅手机↔PC 间实时传输，PC 侧只进本地模型/本地 STT，**不**上传任何第三方（DeepSeek 只收到脱敏文本摘要，延续大脑管线）。
2. **中继不可信**：中继是纯透传管道，不存储、不解析音频内容；TLS 传输 + token 鉴权强制，E2EE 默认开启（配对密钥派生）。
3. **受控注入延续**：语音发起的任务拆解复用 brain 的确认后注入（N-1/N-2/N-3），语音只负责"说 + 听"表达层，不改变注入安全边界。

---

## 2. 版本归属与里程碑

> 升级背景：O-014 原定"近期=飞书语音、远期=V2 自研 App"。用户明确手机语音全双工为核心方向 → **自研手机端提前进 V1.5 主线**；飞书保留为**文本推送**通道（O-002 已选型），不再开发"飞书语音消息"路径。

| 版本 | 内容 | 变化 |
|---|---|---|
| V1 | 监控闭环（已有） | 不变 |
| **V1.5（主线升级）** | 大脑闭环 + **手机语音全双工**（本 spec）+ 单向报告 | 新增手机语音主线；飞书语音路径取消（降为文本推送） |
| V2 | 管家 + 双向远程指挥（任务编排/全自动注入/中继扩展） | 语音中继/双向音频能力 V1.5 已落地，V2 扩展为通用双向指令通道 |

### 2.1 里程碑拆解

| 里程碑 | 范围 | 验收门槛 | 预估 |
|---|---|---|---|
| **M1 手机 App 骨架 + 唤醒** | Android 工程 + 前台服务常驻麦克风 + sherpa-onnx KWS 唤醒 + 悬浮窗/通知状态 + 六态 UI（无音频上行） | 唤醒率/误触发/功耗达标（§12 M1）；进程不被系统杀（48h 待机） | 1.5-2 人周 |
| **M2 流式音频双向（半双工）** | 云端中继 + PC voice 网关（路径 B：sherpa STT → 大脑 intent → TTS）+ 手机 AudioTrack 下行 + 局域网直连 | 端到端 P50 ≤ 2.5s（V-4）；断线重连 ≤ 30s | 1-1.5 人周 |
| **M3 全双工打断** | 路径 A 本地模型原生全双工（APM）+ barge-in（手机 VAD 打断 + PC 侧原生打断）+ 唤醒词→大脑 hook | 端到端 P50 ≤ 1.5s（V-2）；打断 < 500ms（V-3）；可连续打断 3 次 | 1-1.5 人周 |

> 依赖顺序：M2 依赖 M1（App 骨架）；M3 依赖 M2（流式管道）+ PoC B3（APM 全双工实测）。PoC B3 未过或原生延迟 >2s → 按 ADR-003 替代触发条件：M3 降级为"路径 B 半双工 + 手机侧打断"，全双工延后 V1.6（接口不变，仅引擎切换）。

---

## 3. 总体架构

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│        手机端 Android App     │        │       电脑贾克斯（Windows）    │
│                             │        │                              │
│  ┌───────────────────────┐  │  ①WS   │  ┌────────────────────────┐  │
│  │ VoiceForegroundService│  │ 上行    │  │ voice/ 网关（新）        │  │
│  │  ├ MicRecorder        │──┼──audio─┼─▶│  ├ relay_client         │  │
│  │  │   (AudioRecord16k) │  │  下行   │  │  ├ ws_server(局域网直连) │  │
│  │  ├ WakeWordEngine     │◀─┼──audio─┼──│  ├ session 管理          │  │
│  │  │   (sherpa-onnx KWS)│  │        │  │  └ bargein 监听          │  │
│  │  ├ VadEngine(silero)  │  │        │  └────────┬───────────────┘  │
│  │  ├ AudioUplink/TTS    │  │        │           │ ②路径 A/路径 B    │
│  │  ├ BargeIn            │  │        │  ┌────────▼───────────────┐  │
│  │  └ VoiceSession(六态) │  │        │  │ ③ 本地模型 APM(llama)   │  │
│  ├ net/RelayClient(OKHttp)│ │        │  │   全双工（PoC B3）       │  │
│  ├ ui/FloatingOverlay    │  │        │  ├ ④ sherpa STT+edge-tts  │  │
│  └ config/VoiceConfig    │  │        │  └────────┬───────────────┘  │
└────────────┬──────────────┘        │           │⑤ brain intent API  │
             │                       │  ┌────────▼───────────────┐    │
             │  ②局域网直连(同Wi-Fi)   │  │  brain/（已有 V1.5）    │    │
             │                       │  │  DeepSeek 拆解/指令     │    │
             │ ①外网经中继            │  └────────────────────────┘    │
     ┌───────▼───────────┐          └──────────────────────────────┘
     │  云端 WS 中继（新）  │
     │  TLS + token 鉴权  │
     │  纯透传不解析内容    │
     │  (E2EE 时只见密文)   │
     └───────────────────┘
```

**数据流（路径 B，M2 先落地）**：
`手机 Mic 16k PCM → WS 二进制帧 → 中继/直连 → PC voice 网关 → sherpa-onnx STT 文本 → brain intent API（复用大脑）→ 回复文本 → edge-tts / 模型 TTS → PCM → 下行流 → 手机 AudioTrack`

**数据流（路径 A，M3 主路径）**：
`手机 Mic 16k PCM → 中继 → PC voice 网关 → llama.cpp-omni APM prefill(audio)+decode（原生全双工）→ TTS 音频流 → 下行 → 手机 AudioTrack；用户开口 → 手机 VAD 打断 + APM 原生 barge-in`

---

## 4. 手机端（Android App）设计

### 4.1 前台服务 + 常驻麦克风（"一直在听"的 Android 实现）

| 项 | 设计 |
|---|---|
| 服务形态 | `VoiceForegroundService`：`startForegroundService()` 启动，`startForeground()` 常驻通知（低优先级 `IMPORTANCE_LOW`），类型 `foregroundServiceType="microphone"` |
| 采集 | `AudioRecord(MediaRecorder.AudioSource.MIC, 16000, CHANNEL_IN_MONO, ENCODING_PCM_16BIT)`，buffer 20-40ms 帧循环读取，专用采集线程 |
| 通知 | 常驻通知"贾克斯正在聆听"（可折叠）；权限：Android 13+ `POST_NOTIFICATIONS` 运行时申请 |
| 关键权限/声明 | `RECORD_AUDIO`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MICROPHONE`（Android 14+）、`INTERNET`、`SYSTEM_ALERT_WINDOW`（悬浮窗）、`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`（引导式，不静默申请） |
| 保活 | 前台服务即系统保活核心；另需引导用户关闭该应用的系统电池优化（白名单）。**不**用双进程/隐藏服务等灰色手段（合规、稳定） |
| 采集线程读到的帧分发 | 同一帧三路消费：① KWS 唤醒检测 ② VAD 语音活动/打断 ③ 唤醒后上行编码（唤醒前**不**上行，省带宽+隐私） |

**Android 平台约束诚实声明（必读）**：
- Android 10+ 前台服务使用麦克风必须声明 `microphone` 类型；**Android 14 起禁止应用从后台启动 mic 类型前台服务**（仅前台 Activity、通知点击、已授权场景可启动）——因此开机自启需用户手动允许厂商自启动白名单，App 提供"引导页"一键跳转。
- 系统省电/厂商后台限制（MIUI/EMUI/ColorOS 等）可能仍会杀进程；以引导白名单 + 前台服务双重保障，M1 需 48h 待机实测。

### 4.2 唤醒词检测（"随时唤醒"）——选型对比与结论

约束：**离线、低功耗、误触发低、中文唤醒词"贾克斯"、免商业许可（契合"省钱到极致/自托管"）**。

| 方案 | 许可 | 离线 | 中文自定义唤醒词 | 功耗/延迟 | 结论 |
|---|---|---|---|---|---|
| **sherpa-onnx Keyword Spotter** | Apache-2.0（模型 wenetspeech 亦 Apache-2.0） | ✅ | ✅ **免重训**（text2token 配置关键词，tokens-type=ppinyin） | 低；ONNX CPU，3.3M 模型，<100ms/帧窗 | **主选**：与 PC 降级链同框架（sherpa-onnx 1.13.2 已装）、官方 Android APK/JAR/so 齐备、中文原生支持 |
| Porcupine (Picovoice) | 非商用免费 / 商用付费；运行需 AccessKey | ✅（推理离线，初始化一次联网） | ✅ 控制台自训练秒级 | 极低（<1MB，97.1% 精度@1FA/10h，3.8% CPU） | 备选：若 KWS 精度/功耗不达标再评估；注意商用授权成本 |
| Vosk (grammar KWS) | Apache-2.0 | ✅ | ✅（grammar 限定词表） | 中（2MB+，300ms，KWS 非主业） | 否决：KWS 是 ASR 附属能力，功耗/误触发一般 |
| openWakeWord | Apache-2.0 | ✅ | ⚠️ 预训练仅英文，中文需自训练 | 低（ONNX 量化） | 否决（本 MVP）：中文需自训练（§14 out-of-scope） |
| snowboy | 已停维护（KITT.AI 被收购 2019） | ✅ | ✅ | 低 | 否决：停维护，Android 兼容风险 |
| PocketSphinx | BSD | ✅ | ✅ | 中 | 否决：精度低（52%@benchmark） |

**结论**：唤醒词主选 **sherpa-onnx KWS**（模型 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`，关键词表 `["贾克斯","小贾"]`）。原因：① 中文免训练（配置即用，不踩"自训练模型"坑）；② 与 PC 降级链同一运行时，团队技能复用；③ Apache-2.0 免商业许可；④ 官方提供 Android 预编译（jar + jni + assets）。Porcupine 作为**精度兜底备选**（M1 实测 KWS 误触发 >1 次/天 或 功耗超标时启用，需用户确认商用授权成本）。

### 4.3 录音流上行（AudioRecord → 编码 → WS 流式）

- **默认编码：PCM 16kHz 16-bit 单声道**（32KB/s，任何网络可承受；与 VAD/ASR/APM 原生格式一致，省去编码/解码一跳）。
- Opus 可选：网络受限时协商 `audio.format=opus_16k`（Concentus 纯 Java 编码器，避免 NDK 依赖；M3 优化项，非默认）。
- 分帧：20-40ms/帧，二进制 WS 帧带 `seq`（见 §7 帧协议）。
- 唤醒前不上行；唤醒后上行当前 utterance，`speech_end`（VAD 静音）后 PC 端停止接收本轮。

### 4.4 TTS 流下行 + barge-in（"随时打断"）

- 播放：`AudioTrack(MODE_STREAM)`，16kHz PCM 直写；buffer ≥ `getMinBufferSize()*2`（防爆音）。
- 打断判定：Speaking 状态下，采集线程持续跑 **silero-vad**（16kHz，与 PC ADR-003 同模型族）——检出语音活动（先音量粗判 + VAD 确认，防误打断）→ ① `AudioTrack.pause()+flush()`（幂等）② 发 `interrupt` 控制帧 ③ 状态 Speaking→Listening。**目标 <500ms（V-3）**。
- 打断语义："变形不重置"依赖 PoC B3 实测（ADR-003 备注）；M3 若原生打断不稳，手机侧 VAD 打断 + PC 侧路径 B 重开会话兜底。

### 4.5 UI：悬浮窗 + 通知栏状态 + 语音波形

- **悬浮窗**（`SYSTEM_ALERT_WINDOW`）：常驻小圆球（对齐桌宠视觉语言），状态色 = 六态（idle/monitoring/listening/thinking/speaking/alerting，复用 `pet_state` 枚举与语义色）；听/说时展开显示语音波形（音量 RMS 实时绘制，Canvas 自绘，不引图表库）。
- **通知栏**：常驻通知（服务生命周期）+ 可操作按钮（暂停监听 / 立即对话 / 退出）。
- **图标约束**：遵循 project 统一 SVG 图标库约束（无 emoji 作图标、无紫→粉渐变、无 hex 字面量硬编码）；Android 端以 vector drawable（SVG 等价）落地同一套图标。

### 4.6 手机端六态状态机（对齐 PRD pet_state）

```
monitoring ──唤醒词命中──► listening ──VAD语音结束──► thinking ──TTS开始──► speaking
    ▲                          │  ▲                        │                    │
    └────────静默超时(15s)────────┘  │(barge-in <500ms)       │(TTS 结束)          │
    ◄───────────────────────────────┴────────────────────────┴──────────────────┘
    （speaking → 用户开口 → listening；thinking → 用户开口 → listening）
```
事件源：KWS 命中 / VAD / 下行 session_state / 本地超时；状态变化实时驱动悬浮窗与通知。

---

## 5. 平台约束与功耗预算（诚实声明）

### 5.1 iOS

**iOS 后台常驻麦克风监听受限（系统硬限制）**：第三方 App 无法在后台持续访问麦克风（`AVAudioSession` 后台录音仅限特定场景且受时长限制），无前台服务机制。结论：**iOS 不在 V1.5 范围**；若未来需要，形态改为"前台激活时语音对话 / Siri Shortcuts 触发 / 通知点击启动"，非"一直在听"。已写入 §14 out-of-scope。

### 5.2 功耗预算（估算，M1 实测校准）

| 项 | 估算 | 说明 |
|---|---|---|
| AudioRecord 16k 常驻采集 | 0.5-1.5%/小时（息屏） | 现代机型（骁龙/猎户座）麦克风 DMA 开销小 |
| KWS 推理（3.3M ONNX，CPU） | 1-3%/小时 | 帧窗 40ms、灵敏度 0.5；机型相关 |
| **合计常驻监听** | **约 5-15%/天**（非连续对话） | 依赖屏幕状态/唤醒次数/机型，需 M1 真机实测 |
| 一次语音问答（30s） | 0.1-0.3% | 对话期额外开销可忽略 |

**降功耗策略（内置开关）**：
1. **省电模式（默认关）**：息屏 + 无近场使用超 10min → 暂停 KWS，退化为"轻触悬浮窗唤醒"（§5.3 交互兜底）。
2. KWS 帧窗 40ms、灵敏度可调（0.3-0.7），降低误触发同时省电。
3. 唤醒后对话期保持全速；空闲回落 KWS 轻量模式。
4. 电量 <15% 自动暂停监听，仅保留通知入口。

### 5.3 交互兜底（KWS 关闭/被系统杀时）

唤醒方式降级链：**唤醒词（默认）→ 悬浮窗轻触（常驻，几乎无额外功耗）→ 通知栏按钮**。M1 交付时三种入口全部可用。

---

## 6. 云端中继（V2 已定方向，V1.5 提前落地）

### 6.1 定位与原则

- **纯透传管道**：只负责把手机连接与 PC 连接按会话配对并双向转发帧，**不解码、不解析、不存储**音频内容。
- **传输安全**：WSS（TLS）强制 + token 鉴权（配对时签发）；E2EE 默认开启（§6.4）时中继只见密文。
- **解耦电脑公网暴露**：电脑不在公网开端口（安全性优先）；中继为唯一外网入口。

### 6.2 选型对比

| 方案 | 成本 | 适合 | 结论 |
|---|---|---|---|
| **轻量云服务器自建 WS 中继**（FastAPI + websockets，Docker） | ¥40-80/月 | 单用户长连接全双工、可控、可加 E2EE、可观测 | **主选** |
| frp/内网穿透直连电脑 | 免费-¥20/月 | 电脑常开（本项目电脑 24h 监控，天然常开） | 备选：零中继成本但需电脑暴露端口+公网域名，攻击面更大；作为 M3 后优化项 |
| 腾讯云函数/CloudBase WS | 按量 | 低频短连接 | 否决（当前）：长连接全双工在无状态 FaaS 上成本与复杂度双劣 |

> 补充：**局域网直连（同 Wi-Fi）**：手机→`ws://<PC-LAN-IP>:8000/api/v1/voice/stream` 直连 PC voice 网关，不经中继，延迟更低、零外网依赖。M2 实现，配 `voice.yaml → relay.lan_direct=true` 自动探测。

### 6.3 中继部署规格

```
relay/
├── server.py            # FastAPI + websockets 中继（≤300 行，纯转发 + 会话配对）
├── auth.py              # token 校验（JWT/预共享）、配对码校验（≤150 行）
├── config.yaml          # 端口 / token 有效期 / 会话超时 / 限流
└── Dockerfile           # python:3.11-slim
```
- 会话模型：`session = {session_id, phone_conn, pc_conn, pairing_code, created_at}`；两端连接独立，按 `pairing_code + device_id` 关联。
- 保活：两端每 15s `heartbeat`；60s 无心跳踢连接并通知对端。
- 限流：单 token 并发会话 ≤1（单用户）；帧级不设限（透传）。

### 6.4 传输安全

| 层 | 机制 | 强制 |
|---|---|---|
| 传输 | WSS (TLS 1.2+) | ✅ 强制 |
| 鉴权 | 配对时 PC 生成 6 位配对码 + 签名 token；手机输入配对码换 token | ✅ 强制 |
| 内容加密 | 手机与 PC 由配对码经 HKDF 派生会话密钥，音频帧 XChaCha20-Poly1305（libsodium） | ✅ 默认开启（配置可关，调试用） |
| 隐私 | 中继不落盘、不解析、不日志内容；录音即发即弃 | ✅ 强制 |

---

## 7. 协议设计（WS 帧 + JSON schema，手机↔中继↔PC 三端统一）

### 7.1 连接与握手

```
手机:  wss://relay.example.com/ws?token=<jwt>&role=phone&device_id=<android-id>
PC:    wss://relay.example.com/ws?token=<jwt>&role=pc&device_id=<pc-id>
（局域网直连：PC 侧 ws://<lan-ip>:8000/api/v1/voice/stream 同协议）
```

握手消息（JSON 文本帧）：

```json
{"type":"hello","role":"phone","device_id":"samsung-s24-xxxx","app_version":"0.1.0",
 "pairing_code":"123456",
 "audio":{"dir":"up","format":"pcm_s16le_16k","chunk_ms":40},
 "features":["wakeword","bargein","e2ee"]}
{"type":"hello","role":"pc","device_id":"jax-pc-01","engine":"native|brain","state":"idle",
 "audio":{"dir":"down","format":"pcm_s16le_16k","chunk_ms":40}}
{"type":"ready","session_id":"vs-20260803-001",
 "audio":{"up":"pcm_s16le_16k","down":"pcm_s16le_16k"}}
```

### 7.2 帧格式

| 帧 | 编码 | 载荷 |
|---|---|---|
| 控制帧 | WS 文本帧 = JSON | §7.3 schema |
| 音频帧 | WS 二进制帧 | `[0x02][seq:u32 BE][ts_ms:u64 BE][payload]`（payload=E2EE 密文或明文 PCM/Opus） |

中继对二进制帧**原样透传**，不解析头部（E2EE 时连头部也可整体加密为纯密文载荷，中继只按序转发）。

### 7.3 控制帧 JSON schema（统一 `VoiceControlFrame`）

```yaml
# 上行（手机 → PC）
wake:            {type: wake, ts: unix_ms, sensitivity_hint?: float}
speech_start:    {type: speech_start, ts: unix_ms}          # VAD 起音（可省，以音频流为准）
speech_end:      {type: speech_end, ts: unix_ms, duration_ms: int}
interrupt:       {type: interrupt, ts: unix_ms}             # barge-in 打断
cancel:          {type: cancel, ts: unix_ms}                # 取消本轮
heartbeat:       {type: heartbeat, ts: unix_ms}             # 15s/次

# 下行（PC → 手机）
session_state:   {type: session_state, state: listening|thinking|speaking|monitoring, ts: unix_ms}
transcript:      {type: transcript, text: string, is_final: bool}   # 可选：ASR 中间文本（手机悬浮窗显示）
audio_start:     {type: audio_start, format: pcm_s16le_16k, seq: int}
audio_end:       {type: audio_end, seq: int, reason: done|interrupted|error}
brain_preview:   {type: brain_preview, task_id: string, preview: string}  # 大脑拆解结果语音播报前的文本（可选）
error:           {type: error, code: string, message: string}
```

### 7.4 会话语义

- 一轮对话 = `wake` → 音频流（speech_start...speech_end）→ `session_state: thinking` → `audio_start` → 下行音频流 → `audio_end(done)` → `session_state: monitoring`（或等待下一句）。
- 打断 = Speaking 中收到 `interrupt` → 立即 `audio_end(interrupted)` + `session_state: listening`，PC 侧 APM 原生 barge-in 或路径 B 重开会话。
- 错误/超时：PC 侧 60s 无响应 → `error(code=timeout)`；手机 15s 静默（V-5）→ 回落 monitoring。
- 断线重连：指数退避（1s→2s→4s→8s→…上限 30s），重连后重新 `hello` 重建会话；PC 侧任务不因手机断线中止（R-3 语义延续）。

---

## 8. PC 贾克斯 voice 网关（backend/app/voice/，新增）

### 8.1 目录与文件（遵循 code-organization 硬规则：单文件 ≤300 行、入口只装配）

```
backend/app/voice/
├── relay_client.py        # 出站中继客户端（WSS，token，心跳/重连；trust_env 显式关闭）
├── ws_server.py           # 局域网直连 WS 端点 /api/v1/voice/stream（同协议）
├── session.py             # VoiceSession 管理：手机连接 ↔ 本地引擎桥接、会话生命周期
├── apm_bridge.py          # 路径 A：llama.cpp-omni APM 全双工（prefill(audio)+decode 流）
├── half_duplex.py         # 路径 B：sherpa-onnx STT → brain intent → edge-tts/模型 TTS
├── bargein.py             # 下行 TTS 时监听上行音频 → 中断（PC 侧兜底）
├── audio.py               # PCM 分帧/采样率/格式转换工具（纯函数）
├── schemas.py             # VoiceControlFrame / AudioChunk 等 Pydantic 模型
└── config.py              # 读 config/voice.yaml（或并入现有 config.py Settings）
backend/app/api/routes_voice.py   # 控制面：配对码签发 / 状态查询（≤200 行，只编排）
backend/app/main.py               # 改造：lifespan 装配 voice 网关 + include_router
config/voice.yaml                 # 新增：voice 网关配置（§8.4）
```

依赖方向：`routes_voice / relay_client / ws_server → session → apm_bridge / half_duplex → audio / schemas`；`half_duplex → brain.intent_service（复用大脑）`；`apm_bridge → engine.llama_omni_client（复用 SSE 客户端）`。**禁止**反向 import。

### 8.2 路径 A：本地模型原生全双工（`apm_bridge.py`，主路径，M3）

```
手机音频帧 → session → apm_bridge：
  init_session(media_type=1, use_tts=true, duplex_mode=true) → [内部完成 cnt=0 prefill]
  → 循环 ~1000ms：prefill(audio_path_prefix=1s 16k WAV 块, img_path_prefix=截图可复用, cnt=N 递增)
  → decode(debug_dir, stream=true) 并行消费 SSE → tts_wav 文件监听 → PCM 下行
  用户开口 → APM 原生 barge-in（ADR-003 已选型，原生打断）
```
> **`{{POC-B3}}` 回填（2026-08-05 实测 + 官方契约 taowen/llama.cpp-omni）**：
> - 音频承载：**1s 16k PCM WAV 块实时流**（prefill 循环，cnt 从 1 递增会话内不重置；init 内部完成 cnt=0，勿重复发）
> - `use_tts: true`、`duplex_mode: true`、`media_type: 1`；静音块也要发（保双工节奏）
> - decode 音频输出**不在 SSE 流**：传 `debug_dir`，TTS WAV 增量写 `debug_dir/round_XXX/tts_wav/wav_*.wav`，**文件监听播放**
> - SSE 仅文本：`{"content": 增量, "is_listen": bool, "stop": bool}`；is_listen=true 停播切监听
> - **打断**：TTS 播放中喂新音频（VAD 检测）→ APM 原生 barge-in（"压扁"而非排队）
> - 实测 TTFT 875–1034ms（3060，GPU 空闲）达标 ≤1.5s；prefill 偶发跳过 + decode 长连接需流水线化（详见 docs/poc/POC-003-voice-duplex.md 阻塞项）
> - 资源互斥：APM 与监控共用 19080，`_llm_lock` 互斥 + 监控降频（§10）

### 8.3 路径 B：半双工降级链（`half_duplex.py`，M2 先落地，ADR-003 兜底）

```
手机音频帧 → sherpa-onnx 流式 STT（SenseVoice，CPU 60-200ms 首字）→ 文本
  → voice 网关判定：casual 问答 → 本地模型 chat / 或 brain intent（"帮我/拆解/写/重构" 等触发词）
  → 回复文本 → TTS（模型原生 TTS 优先 / edge-tts zh-CN-XiaoxiaoNeural 兜底）→ PCM → 下行帧
```

**唤醒词 → 大脑 hook（需求 ④）**：唤醒后的意图直接进**现有 brain intent API**（`POST /api/v1/brain/intent` → `POST /api/v1/brain/task`），语音仅作为表达层；拆解结果文本经 TTS 播报 + 桌宠确认卡（注入边界不变）。路由规则：
- `voice.yaml → path: auto`：`half_duplex` 对 STT 文本做轻量触发词分类（`帮我|拆解|重构|实现|修|写|优化|测试` → brain；其余 → 本地模型直接回答）。
- M3 后：路径 A 的 APM 文本侧同样接入该判定（APM 输出文本 → 触发词 → brain），实现"全双工听 + 大脑拆解 + TTS 播报"一体化。

### 8.4 配置 `config/voice.yaml`

```yaml
voice:
  path: auto                      # auto|native|brain
  relay:
    url: wss://relay.example.com/ws
    token_env: VOICE_RELAY_TOKEN  # 仅 .env，不入库
    lan_direct: true              # 同 Wi-Fi 自动直连
    heartbeat_s: 15
    reconnect_backoff_s: [1, 2, 4, 8, 16, 30]
  e2ee:
    enabled: true
    kdf: HKDF-SHA256
  bargein:
    vad_model: silero_vad_v6_1_onnx   # 与手机端同模型族
    interrupt_ms_budget: 500
  half_duplex:
    stt: sherpa-onnx-1.13.2
    stt_model: sensevoice-streaming   # ADR-003
    tts: model|edge-tts
    edge_tts_voice: zh-CN-XiaoxiaoNeural
  session:
    idle_timeout_s: 15               # V-5
    max_round_ms: 60000
```

---

## 9. 状态与监控（复用大脑/推送基建）

- `voice` 网关状态并入 `GET /api/v1/status`：`voice: {relay: connected|disconnected, engine: native|brain|off, phone: online|offline, path: A|B}`。
- 事件广播：`session_state` 变化经 EventBus 广播（`EVT_PET_STATE` 复用），桌宠与手机悬浮窗同源同步。
- 埋点（对齐 PRD §10）：`voice_wake_count` / `voice_round_ms(P50)` / `voice_interrupt_ms(P50)` / `voice_error` / `voice_battery_pct`。
- 报告：关键事件（唤醒成功/对话失败/大脑降级）经现有 PushManager 脱敏推送（P-1），飞书文本通道即用。

---

## 10. 机器可读产出物（openapi.yaml 修订点）

> 与后端 brain spec 同机制：本 spec 为契约真源，实施阶段同步修订 `docs/openapi.yaml`，全部新增端点标 `x-phase: v1.5`、`x-implemented: false`。

```yaml
# 新增 paths（控制面；实时音频走 WS，不走 REST）
POST /api/v1/voice/pair           # 生成配对码 + 签发 token（手机扫码/输入码换 token）
GET  /api/v1/voice/status         # 语音网关状态（relay/phone/engine/path）
# 新增 WS 端点（局域网直连）
WS   /api/v1/voice/stream         # 手机直连 PC 的语音流（§7 协议）
# WS /ws/pet 契约新增事件
#   {type:"event", event:"voice_state", data:{state, engine, path, transcript?}}
```

---

## 11. 已知坑（内嵌硬约束，照做避开）

1. **FGS mic 类型**：AndroidManifest 必须 `foregroundServiceType="microphone"` + `FOREGROUND_SERVICE_MICROPHONE` 权限；Android 14 禁止后台启动，开机自启靠厂商白名单引导（§4.1）。
2. **厂商省电杀后台**：不静默申请 `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`，提供引导页；48h 待机验收（M1）。
3. **AudioSource 选 MIC**：勿用 `VOICE_COMMUNICATION`/`VOICE_CALL`（会被系统 AEC/NS 处理，破坏原始 16k 流）。
4. **AudioTrack 爆音**：buffer ≥ `getMinBufferSize()*2`；`write()` 不超过 buffer 长度；打断时 `pause()+flush()` 幂等。
5. **WebSocket 断线**：指数退避重连 ≤30s；音频帧带 `seq`，PC 端拼接 + 乱序检测（§7.2）。
6. **打断竞态**：Speaking 中 VAD 命中 → 先置状态 Listening 再 `pause()+flush()`，避免半帧混音；音量粗判 + VAD 确认双门限防误打断。
7. **唤醒误触发**：灵敏度默认 0.5，M1 用 10min 电视/人声噪声实测校准；可调 0.3-0.7。
8. **中继无状态**：两端连接独立，按 `pairing_code` 关联；中继重启/断连 → 手机自动重连并重新 `hello`（幂等）。
9. **代理坑（项目已踩）**：PC 侧 `relay_client` 与 DeepSeek 客户端同规则——`httpx/websockets` **不隐式信任环境代理**（`trust_env` 显式关闭），避免 127.0.0.1:7890 残留导致中继连不上。
10. **APM 显存互斥**：路径 A 与监控共享 `_llm_lock`（backend-llama-client-spec §5 契约），对话期监控降频 10-15s/帧（PRD M-3），不并发双实例（ADR-001）。
11. **隐私**：录音不落盘、不进日志、中继不存储；E2EE 默认开；`VOICE_RELAY_TOKEN` 仅 `.env`。
12. **时钟不同步**：音频帧 `ts_ms` 仅统计用，播放由音频流驱动，不依赖同步。

---

## 12. 验收清单（照做）

### M1（App 骨架 + 唤醒）
- [ ] APK 侧载安装；授予 mic/通知/悬浮窗权限 + 电池白名单引导页可用
- [ ] 前台服务常驻：48h 待机进程存活（无自启动白名单场景至少到厂商限制边界如实记录）
- [ ] 唤醒词：说"贾克斯" → 悬浮窗/通知进入 Listening（P95 < 300ms）；10min 噪声 0 误触发（灵敏度 0.5）
- [ ] 三种入口可用：唤醒词 / 悬浮窗轻触 / 通知按钮
- [ ] 功耗：8h 待机电池下降 ≤ 8%（真机实测记录，机型注明）
- [ ] 图标/状态色遵循统一 SVG 图标库约束（无 emoji 作图标、无紫→粉渐变）

### M2（流式双向）
- [ ] 中继部署：手机 4G（异网）→ 中继 → PC 语音问答成功；局域网直连同 Wi-Fi 成功
- [ ] 路径 B 端到端 P50 ≤ 2.5s（V-4）；断线（杀中继）→ 手机 30s 内自动重连并恢复会话
- [ ] 大脑 hook：说"帮我把数据层拆成接口+实现" → brain intent/task 管线被调用 → TTS 播报拆解结果 + 桌宠确认卡
- [ ] E2EE：抓包确认中继侧只见密文；`VOICE_RELAY_TOKEN` 不出现在日志/DB

### M3（全双工打断）
- [ ] 路径 A 端到端 P50 ≤ 1.5s（V-2）；打断 < 500ms（V-3）；可连续打断 3 次
- [ ] 对话期监控降频生效（M-3）；监控与语音并发不崩
- [ ] 15s 静默回落 monitoring（V-5）
- [ ] PoC B3 未过时：M3 降级为半双工 + 手机侧打断（§2.1 替代触发），接口不变

### 全局
- [ ] 单测全绿（Android JVM 单测 + backend pytest 新增 voice 用例）；`wc -l` 最大文件 ≤ 300 行
- [ ] 隐私审计：录音零落盘、零日志；中继零解析零存储

---

## 13. 端到端验证（E2E，照做）

1. **M1**：装 APK → 引导页授权（mic/通知/悬浮窗/电池白名单）→ 启动 → 通知"贾克斯正在聆听" → 说"贾克斯" → 悬浮窗 Listening 动效 → 录屏确认；放 10min 电视噪声 → 无唤醒。
2. **M2**：起中继 + PC 后端（voice 网关启动，`/api/v1/voice/status` relay=connected）→ 手机 4G 下说"贾克斯，现在几点了" → 手机听到语音回答（记录端到端耗时）；杀中继进程 → 手机 30s 内重连 → 再说一句恢复。
3. **M2 大脑**：说"贾克斯，帮我把这个项目的数据层拆成接口+实现" → 手机听到拆解播报 → 桌宠弹出确认卡 → 确认后注入 Codex（复用 brain E2E §13 大脑 spec）。
4. **M3**：手机 Speaking 播放 TTS 时说"停" → 500ms 内停止 → 接着说"改成另一个项目" → 正常续答（连续 3 次打断）；10 轮对话 P50 ≤ 1.5s（脚本计时）。
5. **降级**：停本地模型服务 → 说"贾克斯" → 走路径 B（STT+brain+TTS）仍可对话；重启模型 → 恢复路径 A。
6. **回归**：`cd backend && pytest -q` 全绿；既有 V1/V1.5 用例零回归。

---

## 14. Out-of-Scope（明确不做）

1. **iOS 后台常驻监听**——系统硬限制，仅 Android（§5.1）。
2. **唤醒词自训练模型**——sherpa-onnx 免训练自定义关键词已覆盖；若 M1 精度不达标，先评估 Porcupine 商用授权，再考虑自训练（延后决策）。
3. **飞书语音消息双向路径**——飞书保留为**文本推送**（O-002）；原 O-014"飞书语音→大脑"不再开发。
4. **多用户/多设备**——单用户：1 手机 + 1 PC。
5. **语义唤醒 / 声纹识别 / 多说话人区分**。
6. **录音留存与历史回放**——录音即发即弃，不做存储/分析。
7. **远场降噪（AEC/NS 增强）**——MVP 用系统 mic 原始流；AEC 增强留 V2 可选。
8. **中继高可用 / 多地域 / 负载均衡**——单用户单实例。
9. **通用双向指令通道（手机→PC 任意指令）**——V1.5 仅语音；R-* 双向远程指挥仍属 V2。
10. **App Store 发布 / 签名上架**——Android 侧载 APK 即可；上架流程留 V2。

---

## 15. 开发成本估算（参考 references/cost-models/development-costs.md）

| 项 | 人周（AI 辅助） | 说明 |
|---|---|---|
| M1 App 骨架 + 唤醒 | 1.5-2 | 前台服务 + KWS 集成 + 悬浮窗 UI |
| M2 流式双向 + 中继 | 1-1.5 | 中继 ~0.5 周 + voice 网关 + 半双工 |
| M3 全双工打断 | 1-1.5 | APM 桥接 + barge-in（依赖 PoC B3） |
| 测试/回归/隐私审计 | 0.5-1 | 单测 + E2E + 抓包审计 |
| **合计** | **4-6 人周** | 云中继服务器 ¥40-80/月（若自建） |
| 对比 | 自研 STT/TTS/打断管线 10+ 人周 | 模型原生全双工 + sherpa/edge 降级链大幅压降 |

---

## 16. 升格建议（供决策）

| 决策 | 建议 | 理由 |
|---|---|---|
| 手机语音全双工 = V1.5 主线 | ✅ 升格（替代飞书语音路径） | 用户核心诉求，技术已验证可行（ADR-003），与大脑闭环天然衔接 |
| 唤醒词 sherpa-onnx KWS | ✅ 主选 | 中文免训练 + 与 PC 降级链同框架 + Apache-2.0 |
| 中继提前进 V1.5 | ✅ 落地（V2 原定方向提前） | 手机异地可用是语音主线前提；纯透传管道成本极低 |
| 建议新增 ADR-011（手机语音架构） | ✅ 建议 | 本文档 §2/§4.2/§6 决策需落 ADR 防推翻 |

> 依据本 spec 定稿后，由总监裁决升格并升格为 ADR-011；本文档作为实施契约照做。
