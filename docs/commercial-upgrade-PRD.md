# 商业升级 PRD 基线：Windows 桌面宠物 AI + Android 全双工语音助手

> 版本：Phase 1 产品调研与 PRD 基线
> 日期：2026-08-07
> 范围：单用户、1 台 Windows 电脑、1 台 Android 手机
> 状态：产品基线，不代表实时音频、Android 真机或 TRTC 跨端能力已经通过

## 1. 产品定位与问题陈述

用户需要的不是一个会说话的桌宠，而是一个跨设备的实时工作陪伴：当用户离开 Windows 键盘、切换窗口或同时运行多个 AI 编程 agent 时，仍可通过 Android 手机连续、低延迟、可打断地对话，并了解 Codex、Trae、Hermes、WorkBuddy 的工作状态。

当前方案的桌宠和本地视觉监控能提供状态入口，但 Android 到 Windows 的全双工语音闭环尚未被真实跨端证据证明。现有测试主要覆盖后端健康、状态、提醒和推送，无法证明 Android 收到并播放了 AI 回复。继续把“可连接”当作“可对话”，会让商业交付在第一句或第二句语音处失败。

产品价值应聚焦于：

1. 手机随时发起或继续语音会话。
2. 用户说话时 AI 可自然响应，AI 说话时用户可打断。
3. 桌宠显示监控、聆听、思考、说话、异常等状态。
4. 只把用户允许的 agent 状态和脱敏摘要用于对话，截图默认留在本机。
5. RTC、模型、网络、权限异常都有明确原因、恢复动作和可审计证据。

## 2. 行业基线

目标知识库 `references/industries/ai-assistant.md` 不存在。本基线改读最接近的 AI 原生行业文档：

`C:\Users\Administrator\.workbuddy\plugins\marketplaces\experts\plugins\mvp-dev-expert-team\references\industries\ai-native.md`

采用的行业基线：

- AI 能力应可感知，生成结果应建立信任。
- 必须覆盖 Loading、Empty、Error、Populated、Edge 五类状态。
- 生成过程中展示进度、取消入口和预计时间。
- 失败时说明错误原因，提供重试和降级路径。
- 多模型切换、上下文和数据权限应透明。
- 功能图标采用统一 SVG 图标库，具体库由架构师按项目选型锁定；禁止使用 emoji 作为功能图标。
- 设计中不采用紫色到粉色渐变，也不使用空洞的模板化占位文案。

## 3. 当前现状与商业产品差距

### 3.1 核心体验

成熟产品已将同时听说、自然打断、暂停/恢复、后台或锁屏持续对话、转写或字幕做成用户预期。OpenAI 官方 Voice FAQ 还描述了 Work/Codex 中的语音任务启动、进度询问和多 agent 协调能力；Google Gemini Live 支持 Android 后台和锁屏继续。

本项目的 V1.1 语音需求仍是目标规格：首字 P50 ≤1.5s、打断 P95 ≤300ms、静默 15s 回落。审计没有证明 Android 真正收到并播放远端音频，因此不能宣称已经达到 GPTLive 对标体验。

### 3.2 稳定性与生命周期

审计报告给出的生产就绪评分为 42/100，结论为 FAIL。已知问题包括：

- sidecar 声明了 `trtc-electron-sdk: 13.4.802-beta.3`，但实际包文件缺失，`npm ls` 返回 `UNMET DEPENDENCY`。
- Android 在会话签发尚未完成时取消，会将 `rtcExiting=true` 留在永久锁定状态，后续唤醒被拒绝。
- 远端 `audioStatus=2` 被解释为静音并调用 `muteRemoteAudio(true)`，可能阻挡后续 AI 回复。
- sidecar 正常退出只关闭窗口，可能残留 Electron 主进程并造成僵尸进程或多实例竞争。
- 上行和下行 PCM 使用无界队列，网络或模型变慢时会堆积陈旧音频。

### 3.3 音频契约与性能

下行整形器按单个模型块切片，尾部不足 20ms 的数据可能直接发送；缺少跨块 residue 缓存、固定节拍、尾帧策略和背压。商业版需要验证：连接建立、首个远端音频帧、首个可听播放、打断、重连、第二轮播放的 P50/P95，并记录抖动、丢帧和队列延迟。

PRD 中的目标门槛如下，但在真实 E2E 完成前均属于待验收目标：

- 连接成功提示 ≤10s。
- 语音首字 P50 ≤1.5s。
- 用户打断后 P95 ≤300ms 停止当前播放并进入 Listening。
- 监控单帧端到端判定 ≤4s。
- 核心错误出现后 ≤2s 显示分类原因和恢复动作。

### 3.4 安全、权限与隐私

`backend/app/api/routes_voice.py` 的会话签发接口当前仅校验 ID 格式，缺少设备凭证、sidecar 服务身份、请求签名、nonce、重放防护和限流。任何可访问端点的调用方都可能为合法格式的 `device_id/user_id` 申请短期凭证。

桌面 PRD 已定义截图不出本机、云端仅接收脱敏会话摘要，不含代码原文、文件路径和 API key。商业版数据边界进一步冻结为：

- 转写默认不持久化；只有用户主动开启“保存转写”后，才允许在本地加密保存。
- 用户可随时在本地删除或导出已保存转写；关闭保存开关不上传历史内容。
- 诊断导出仅包含脱敏指标和事件（如 `session_id`、时间、状态、延迟、帧计数和错误类别），不包含凭证、原始音频、截图、代码、文件路径或完整敏感文本。
- 原始音频不持久化；截图默认仅本机分析。第三方模型或 RTC 服务只接收产品明确允许的最小数据，第三方云端开关必须可见且可关闭。
- 用户可查看设备列表并撤销 Android 设备或 sidecar 权限；撤销后立即拒绝新会话和新凭证。
- 关闭麦克风、后台对话、桌面捕获或第三方云端开关后，新音频/截图/云端上传必须即时停止；当前会话按用户选择结束或保留非敏感本地状态。

### 3.5 异常处理、兼容性与可维护性

现有后端测试通过不能代表核心双工可用。当前没有覆盖以下真实路径：连续两轮语音、快速点击、签发取消、重进会话、断网恢复、权限拒绝、Android 远端首帧、非零播放和音频路由状态。

仓库还存在交付漂移：移动 README 仍描述旧 WS/0.1.0；根 README 声称可运行 `./gradlew`，仓库缺少 Gradle wrapper；审计中的 Android 构建被 `native-platform.dll.lock` 阻断，未生成可归因当前源码的 APK；`WAKE_DEFAULT_ENABLED=false` 与语音唤醒完成的产品承诺冲突。

需要统一的语音会话生命周期状态为：

`IDLE → SIGNING → ENTERING → IN_ROOM → EXITING → IDLE`

桌面监控状态独立于语音会话生命周期，不使用 `MONITORING` 替代会话 `IDLE`。每次进入和退出语音会话都必须回到 `IDLE`，包括签发阶段取消、进房失败、超时和用户主动结束。

并以 `session_id` 贯穿 Android、sidecar、bridge、模型和播放指标；指标至少包含 `connect`、`first_audio`、`interrupt`、`reconnect`、`error`、`upFrames/upBytes`、`downFrames/downBytes`、`queue depth/age`。

## 4. 竞品与替代方案

### 4.1 竞品对比

| 竞品 | 核心功能 | 优势 | 局限或商业提醒 | 定价/来源 |
|---|---|---|---|---|
| ChatGPT Voice / GPT-Live | 全双工同时听说、自然打断、后台对话、文本/图片混合、Search/Memory；Work/Codex 语音可启动任务和询问 agent 进度 | 全双工和 agent 工作流成熟，跨 Android/iOS/web/桌面 | Live 初始不支持视频、屏幕分享和 connected apps；额度按计划和滚动 24h 变化 | OpenAI 官方 FAQ：https://help.openai.com/en/articles/8400625-voice-mode-faq?os=win |
| Google Gemini Live | Android 实时语音、打断开关、暂停/恢复、后台/锁屏继续、字幕/转写、音色、视频/屏幕分享、Connected Apps | Android 系统级可达性和后台体验成熟 | 锁屏可继续但不能从锁屏新建；功能按账号、地区和版本逐步开放 | Google 官方帮助：https://support.google.com/gemini/answer/15274899?hl=en&co=GENIEz |
| Character.AI Calls/Voice | 双向电话式角色对话、文本线程回看、声音创建/上传、私有/公开权限、多语言 | 陪伴人格、声音资产和会话回看体验强 | 更偏娱乐和陪伴，不适合作为桌面 agent 工作监控工具；价格页面需发布前复核 | 官方 FAQ：http://support.character.ai/hc/en-us/articles/23957274129691-Character-Calls-Voice-FAQ；第三方价格参考：https://www.usagepricing.com/blueprint/character-ai |
| Microsoft Copilot Voice | Windows/Edge/M365 入口、联网搜索、语音交互、办公集成 | OS 和办公生态集成强 | 价值依赖 Microsoft 生态；公开价格资料需发布前重新核价 | 公开价格/能力资料：https://frontdeskreview.com/software/ai-chatbot-assistants/microsoft-copilot |

### 4.2 替代方案

| 替代方案 | 可直接满足的需求 | 放弃或增加的代价 |
|---|---|---|
| ChatGPT Voice/Gemini Live + Windows Copilot | 立即获得成熟语音、打断、后台体验和基础桌面入口 | 无法读取任意本地 agent 窗口；云端隐私依赖；无本地桌宠和跨端状态编排 |
| Vapi/Retell 等成熟 Voice AI 平台 | 快速获得 STT/LLM/TTS 编排、监控和可用性基础；公开资料称 Retell 约 1.54s 平均响应、约 $0.07-$0.19/min | 外部音频与数据依赖、持续按量成本；仍需自建 Windows 捕获、Android 播放、权限、桌宠和本地隐私层 |

## 5. 目标用户

### 5.1 主要用户

25-45 岁、Windows AI 编程重度用户，技术水平中高级，同时运行 Codex、Trae、Hermes、WorkBuddy 等桌面 agent。用户经常离开电脑、切换窗口或并行等待任务，需要快速判断 agent 是否推进、卡住或跑偏，并在必要时用手机插话、打断或追问。

### 5.2 次要用户

有隐私顾虑的独立开发者、研究者和自动化重度用户。他们接受单机优先和有限的设备范围，但要求麦克风、桌面捕获、音频、转写及第三方云端权限透明可撤销。

## 6. 关键旅程

1. 安装桌面端和 Android 端。
2. 授予 Android 麦克风、通知、后台运行权限。
3. 授予 Windows 桌面捕获目标窗口权限。
4. 使用一次性配对码绑定唯一 Android 设备。
5. 健康检查显示 SDK、模型、网络、麦克风和播放路由状态。
6. 手机点击开始，状态依次进入 Listening、Thinking、Speaking。
7. 用户在 AI 说话时开口或点击打断，进入 Interrupted 后重新 Listening。
8. 完成至少两轮连续语音，第二轮仍可听。
9. 暂停会话或锁屏继续，按产品限制显示可用性。
10. 结束会话，查看可选转写或诊断，并可删除数据。

任一步发生断网、权限拒绝、SDK 缺失、模型不可用、捕获失败或播放静音，都必须显示原因和可恢复动作，不得伪造健康状态或永久停留在退出中。

## 7. MVP 范围

### 7.1 P0 必须交付

| 能力 | 交付要求 |
|---|---|
| 配对与设备授权 | 一次性配对码、设备凭证、设备列表、撤销、过期和重放拒绝、审计记录 |
| 全双工会话闭环 | Android 与 Windows/sidecar 上下行固定 20ms PCM；可开始、打断、暂停、结束、重进；至少两轮可听 |
| 桌宠与状态上下文 | 常驻透明桌宠展示监控、聆听、思考、说话、异常；仅使用用户允许的脱敏 agent 状态 |
| 降级与可观测 | 分类错误、重试、重连、明确 fallback；`session_id` 全链路关联；音频帧、播放、队列和延迟指标 |
| 隐私控制 | 转写默认不持久化；用户开启后仅本地加密保存并可删除/导出；诊断仅脱敏指标/事件；截图默认本机；第三方云端开关；麦克风、后台对话、桌面捕获可即时关闭；设备列表/撤销 |

### 7.2 P1 / Backlog

- 本地半双工 fallback。
- 手机单向脱敏报告。
- 意图理解、任务拆解、指令生成和确认后注入 Codex。
- 唤醒词默认开启，必须在真机验证后再承诺。
- 多 harness 任务队列、依赖编排和双向远程任务。
- 云端会话历史、每日报告和宠物皮肤商店。

### 7.3 Out-of-scope

多用户、团队或企业 SSO；绕过用户确认的全自动 agent 操控；读取 harness 内部日志或任意键鼠控制；云端保存原始音频、截图或代码；iOS；视频和屏幕分享；宠物养成或社交；token 成本中心；多语言；真实跨端 E2E 完成前宣称 GPTLive 等价。

## 8. RICE 优先级

公式：`Score = (Reach × Impact × Confidence) / Effort`。Confidence 使用 0.9/0.8/0.7 表示 90%/80%/70%；它是需求与风险判断，不是能力已通过证明。

| 功能 | Reach | Impact | Confidence | Effort | Score | MVP |
|---|---:|---:|---:|---:|---:|---|
| 设备配对、凭证、撤销、限流 | 8 | 3 | 0.9 | 4 | 5.40 | P0 |
| 会话状态机、取消、重进、打断 | 9 | 3 | 0.9 | 5 | 4.86 | P0 |
| 固定 20ms PCM、背压、重连 | 9 | 3 | 0.8 | 5 | 4.32 | P0 |
| 可观测指标与诊断导出 | 7 | 2 | 0.9 | 3 | 4.20 | P0 |
| 真实 Android↔Windows 两轮全双工 | 10 | 3 | 0.9 | 8 | 3.38 | P0 |
| 桌宠状态、监控上下文、隐私开关 | 8 | 2 | 0.8 | 4 | 3.20 | P0 |
| 单向手机报告 | 5 | 2 | 0.8 | 3 | 2.67 | P1 |
| 本地半双工 fallback | 6 | 2 | 0.8 | 4 | 2.40 | P1 |
| 意图到指令拆解与确认后注入 | 7 | 3 | 0.7 | 7 | 2.10 | P1 |
| 唤醒词默认开启 | 5 | 2 | 0.6 | 3 | 2.00 | P1 |
| 多 harness 编排与双向远程任务 | 6 | 3 | 0.6 | 9 | 1.20 | Backlog |

## 9. EARS 验收标准

- **配对**：WHEN 新设备输入有效一次性配对码，THEN 仅绑定该 `device_id`，签发短期凭证并可撤销；无凭证、过期或重放请求被拒并记录审计。
- **连接**：WHEN Android 与 Windows 健康检查通过且用户点击开始，THEN 10s 内显示已连接；失败显示分类原因与重试，不能永久停留退出中。
- **真实播放**：WHEN Android 采集用户说话，THEN Android 上行 RMS>0、sidecar `upFrames/upBytes` 增长、bridge 下行指标增长，并在至少 1 台真机收到远端首帧/PCM 和非零播放证据。
- **打断**：WHILE AI Speaking，WHEN 用户开口或点击打断，THEN P95 ≤300ms 停止当前播放并进入 Listening；连续打断 3 次不崩溃，下一轮仍可播。
- **连续会话**：WHEN 同一会话完成两轮回复，THEN 两轮均有 Android 可听证据；正常回复结束不得依赖 `muteRemoteAudio(true)` 恢复。
- **失败恢复**：WHEN RTC、模型或网络失败，THEN 2s 内显示错误类别、`session_id`、重试/结束/降级动作；恢复后不重复旧 PCM，队列有界且受延迟预算约束。
- **权限关闭**：WHEN 用户关闭后台对话、麦克风、桌面捕获或第三方云端开关，THEN 新音频、截图和云端上传即时停止；按选择结束会话或保留非敏感本地状态，绝不上传原始音频/截图。
- **隐私保存**：WHEN 用户未开启“保存转写”，THEN 转写默认不持久化；WHEN 用户开启保存，THEN 仅本地加密保存且用户可本地删除或导出。
- **诊断导出**：WHEN 用户导出诊断，THEN 文件仅含脱敏指标和事件，不含凭证、原始音频、截图、代码、文件路径或完整敏感文本。
- **设备撤销**：WHEN 用户从设备列表撤销 Android 设备或 sidecar 权限，THEN 立即拒绝该设备的新会话和新凭证，并使已有会话按策略结束。
- **捕获降级**：WHEN 监控窗口最小化或捕获失败，THEN UI 显示不可观测/降级，不伪造 agent 健康状态；恢复窗口后重建捕获并记录原因。
- **首次运行与错误态**：WHEN 首次运行，THEN 展示真实空状态与权限说明；模型、网络、权限错误均提供重试和 fallback/禁用说明。

## 10. 非功能与埋点基线

| 类别 | 要求 | 优先级 |
|---|---|---|
| 性能 | 首屏 <3s；监控判定 API p95 <500ms；语音首字 P50 ≤1.5s；打断 P95 ≤300ms；错误反馈 ≤2s | P0 |
| 可用性 | 无单点故障；模型或 RTC 不可用时显示原因并降级；任务状态不伪造 | P0 |
| 安全 | HTTPS、设备凭证、短期签名/nonce、输入校验、速率限制、审计；API key 不入库 | P0 |
| 隐私 | 截图默认不出本机；明确音频/转写保留、删除、导出和第三方云端范围 | P0 |
| 兼容性 | Windows 目标版本与 Android 支持矩阵需在发布前锁定；真机至少 1 台 | P0 |
| 可访问性 | WCAG 2.1 AA 基本合规；键盘可达、对比度合规、reduced-motion | P2 |
| 图标 | 使用统一 SVG 图标库，具体选型由架构师确定；禁止 emoji 功能图标 | P1 |
| 国际化 | 预留 i18n 接口，多语言暂不纳入 MVP | P2 |
| 数据埋点 | 覆盖获客、激活、会话、打断、重连、错误和转写删除 | P1 |

MVP 关键事件：`page_view`、`sign_up_complete`、`first_core_action`、`session_start`、`session_duration`、`voice_connected`、`first_audio_received`、`barge_in`、`session_reconnect`、`transcript_deleted`、`error_occurred`。每个事件附带 `user_id`、`timestamp`、`device`、`version`；不采集 IP、原始音频、截图、代码原文或原始输入内容。

## 11. RoleVerdict

```yaml
verdict: fail
blocking:
  - 违反项: 核心 Android↔Windows 全双工未完成真实跨端播放验收
    证据: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md:74-95,154-167; scripts/e2e_verify.py:1-194
    期望: 至少 1 台 Android 真机连续两轮；上行/下行 frame+byte、远端首帧、非零播放、第二轮可听证据齐全
  - 违反项: sidecar TRTC SDK 实包缺失
    证据: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md:22-37; sidecar/package-lock.json:698-703
    期望: 干净安装 npm ls 退出 0、运行时 getSDKVersion、sidecar smoke 启动成功
  - 违反项: 会话签发取消形成永久退出锁
    证据: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md:39-55; VoiceForegroundService.kt:270-320; RtcClient.kt:329-333
    期望: SIGNING/ENTERING/IN_ROOM/EXITING 串行状态机，未进房取消立即恢复监听，快速点击/超时/重进有测试
  - 违反项: 远端静音事件可能永久阻断 AI 回复
    证据: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md:57-72; RtcClient.kt:231-260
    期望: 远端状态只驱动 UI；打断用显式播放控制；连续两轮回复均可播放
  - 违反项: 公开签发接口无设备身份认证与防重放
    证据: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md:97-111; backend/app/api/routes_voice.py:39-50,133-177
    期望: 设备凭证、sidecar 身份、短期签名/nonce、限流、审计、禁止客户端任意特权 userId
  - 违反项: 交付构建基线不可复现且文档漂移
    证据: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md:146-167,240-244; README.md:47-60; docs/SPEC.md:20-34
    期望: clean checkout 可构建，含 Gradle wrapper 与锁定依赖，文档版本/拓扑/限制与实现一致
advisory:
  - 建议项: 固定 20ms PCM 跨块缓存、队列有界与丢旧保新
    理由: 当前短帧和无界队列会破坏实时节奏并积累陈旧延迟
  - 建议项: session_id 全链路 correlation 与播放证据指标
    理由: 后端测试绿不能解释 Android 实际听到什么
  - 建议项: 明确音频/转写保留、删除、导出与第三方云端开关
    理由: 单用户商业版仍需可解释的数据权限与撤销能力
  - 建议项: 将唤醒词从承诺降级为可选 beta，直到真机验证
    理由: `WAKE_DEFAULT_ENABLED=false` 与当前产品承诺冲突
  - 建议项: 以 ChatGPT/Gemini 后台、锁屏继续、字幕/转写、暂停/恢复为体验门槛
    理由: 已成为成熟语音产品的用户预期
 evidence:
  - artifact_ref: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md
    line: 1-18
    说明: 总体 FAIL 与 42/100 生产就绪结论
  - artifact_ref: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md
    line: 22-37
    说明: sidecar SDK 缺失
  - artifact_ref: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md
    line: 39-72
    说明: 取消死锁与远端静音错误
  - artifact_ref: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md
    line: 74-111
    说明: 真实 E2E 缺失与签发接口无认证
  - artifact_ref: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md
    line: 115-144
    说明: 短帧、僵尸、无界队列、唤醒词默认关闭
  - artifact_ref: docs/AUDIT-PERSIAN-CAT-DUPLEX-2026-08-07.md
    line: 146-179
    说明: 构建/测试事实与成熟度评分
  - artifact_ref: docs/PRD.md
    line: 73-117
    说明: V1/V1.5/V2 路线与排除项
  - artifact_ref: docs/PRD.md
    line: 119-183
    说明: 现有 EARS 与受控注入安全边界
  - artifact_ref: docs/SPEC.md
    line: 8-18,121-143
    说明: 范围、排除项、已知坑、E2E 门
  - artifact_ref: docs/STATUS.md
    line: 25-67
    说明: PoC、构建进度与待办
```

> 结论：该文件是产品决策和商业验收基线。所有竞品能力来自公开页面；本项目任何实时音频、Android 真机、TRTC 跨端能力均未被本文件声称为已通过。
