# OPEN-DECISIONS — 悬而未决决策登记册

> 规范：只追加 + 就地 RESOLVED；每次开工前复现未决项；解决后升格为 ADR。
> 状态：2026-08-03 M0 快照

---

## 未决项

### O-001 全双工语音的 MVP 归属
- 类别：product-scope
- 描述：全双工语音是否进 MVP（V1.1 已定，但若 PoC B3 通过，是否提前并入 V1 开发序）
- 影响：版本节奏与验收范围
- 备选：A) 保持 V1.1；B) PoC 通过后并入 V1 后段
- Resolves when：M1 PoC B3 结果出炉后由项目总监裁决

### O-002 手机推送通道最终选型（2026-08-03 更新：企微→飞书）
- 类别：technical
- 描述：用户手机系统未知；MVP 实现企业微信 + ntfy 双 Provider，是否默认启用企微（需用户提供 webhook）
- 更新：用户确认 **无企微、有飞书、有微信** → 选型改为 **飞书机器人（替代企微）**；微信个人号无官方 webhook 不适用；ntfy 保留为备选
- 影响：推送可达性验证；飞书机器人同时承载"手机语音对话"近期路径（O-014）
- 备选：A) 飞书 webhook 文本推送（近期）+ B) 飞书机器人语音消息双向（O-014 路径）
- Resolves when：用户创建飞书自建应用提供 App ID/Secret 后配置实测
- 类别：technical
- 描述：用户手机系统未知；MVP 实现企业微信 + ntfy 双 Provider，是否默认启用企微（需用户提供 webhook）
- 影响：推送可达性验证
- 备选：A) 企微默认 + ntfy 备选（推荐）；B) 仅 ntfy；C) 加 Bark（iOS）
- Resolves when：用户提供 webhook 或明确手机系统

### O-003 语音唤醒方式
- 类别：product-scope
- 描述：进入 Listening 的方式：点击宠物 / 全局热键 / 唤醒词（唤醒词需额外模型推理）
- 影响：V1.1 交互细节
- 备选：A) 点击宠物 + 全局热键（推荐，零额外推理）；B) 加唤醒词（轻量模型，约 200MB）
- Resolves when：V1.1 开发排期确定

### O-004 被监控应用窗口匹配策略
- 类别：technical
- 描述：三 App 的窗口标题/进程名匹配规则（Codex 终端窗口标题变化、Trae 多窗口），匹配失败时的降级
- 影响：监控稳定性
- 备选：A) 进程名主匹配 + 标题正则（推荐）；B) 用户手动选择窗口（WGC 选择器）
- Resolves when：PoC B2 实测三窗口标题规律后定

### O-005 语音播报 TTS 归属（2026-08-03 审计追加）
- 类别：product-scope
- 描述：PRD §5.1.2 四级提醒含"语音播报"，但 SPEC §1 V1 未列且 voice/ 为空 → V1 是否依赖 TTS 边界不清
- 影响：V1 验收范围
- 备选：A) V1 四级提醒仅"动效+推送"，语音归 V1.1（推荐）；B) V1 引入 edge-tts 轻量播报
- Resolves when：PoC B3 结果 + 用户裁决

### O-006 推送内容隐私边界/脱敏（2026-08-03 审计追加）
- 类别：product-scope
- 描述：PRD 主打"本地隐私"，但推送默认经 ntfy.sh（云端）发送文本+截图出本机，唯一穿透点未声明
- 影响：安全边界与 PRD 承诺一致性
- 备选：A) 企业微信 webhook + 脱敏文本（不含截图、不含敏感代码片段）——**用户已裁决采用**；B) 仅本地提醒不推送
- Resolves when：✅ 已裁决（webhook URL 待用户提供后配置实测）

### O-007 P1 报告/建议的产品形态（2026-08-03 审计追加）
- 类别：product-scope
- 描述：advice_generator 产出的优化建议呈现位置（桌宠气泡/面板/推送/语音）未定义
- 影响：V1.1/V1.2 交互
- 备选：A) 面板时间线展示 + 4 级提醒附带（推荐）；B) 独立报告页
- Resolves when：V1.1 排期确定

### O-008 监控目标扩展（2026-08-03 用户裁决）
- 类别：product-scope
- 描述：用户确认桌面 4 目标在线均需监控：Codex（开源桌面版重点，CLI 未装）+ Trae + Hermes + WorkBuddy。实测进程名：codex.exe / TRAE SOLO CN.exe（原配置 trae.exe 匹配失败）/ Hermes.exe / WorkBuddy.exe
- 影响：monitors.yaml 配置 + B2 窗口匹配校准 + 轮询预算（4 目标 × 6-8s，模型单实例串行）
- 备选：已裁决 4 目标全保留；多进程（Trae 8/WorkBuddy 8/Hermes 6）需标题正则精确匹配
- Resolves when：✅ 已裁决；标题正则待 PoC B2 实测校准

### O-009 D-3 四级递进语义（2026-08-03 审计追加）
- 类别：product-scope
- 描述：PRD §6.2 D-3 "四级渐进打扰递进"语义歧义——"递进"指按严重度定级还是按时间累进？审计发现前端仅二元化实现（level 1/2 与 3/4 同渲染），缺分级依据。
- 影响：提醒分级与桌宠 UI 表现
- 备选：A) 按严重度定级（推荐）：stuck 超时=4 级、off_track=3 级、恢复=1 级；B) 按时间累进（低→高逐级升级）
- Resolves when：✅ 已裁决 A（按严重度定级，已写入 PRD §6.2 D-3 EARS 描述）

### O-010 HomeRail 开源项目参考评估（2026-08-03 用户提出）
- 类别：design-decision-to-evaluate
- 描述：用户建议调研开源项目 HomeRail（github.com/xiaotianfotos/homerail，MIT，2026-07 发布，TypeScript 语音优先 DAG 工作流运行时，本地 homelab/NAS 部署），评估能否复用减少重复开发
- 总监调研结论（2026-08-03）：
  - **理念可借鉴**：① 语音面契约（ASR/TTS/VAD 契约化设计）→ V1.1 语音管线参考；② DAG 显式交接/运行可重放/评分卡 → 我们 EventBus 流水线可补"判定留痕可回放"；③ "注意力稀缺→打扰最小化"与四级渐进打扰设计哲学互相印证
  - **不可复用（技术不重叠）**：① TS 运行时 vs 我们 Python FastAPI——无法嵌入；② 依赖 Docker Worker + Claude Agent SDK endpoint——我们核心是本地 llama.cpp-omni + WGC 屏幕监控；③ HomeRail 无屏幕监控/视觉判定/桌面宠物/渐进打扰——我们核心功能它完全没有，不存在"重复开发"
  - **结论倾向**：不引入为技术依赖（增加 TS+Docker 复杂度），语音面契约设计与可观测理念在 V1.1 时参考
- Resolves when：✅ 已裁决（ADR-009：保留 Python 监控后端、借鉴设计理念不引依赖；语音面契约 V1.1 对照）

---

## 已解决（升格为 ADR）

| ID | 摘要 | 升格 |
|---|---|---|
| O-000 | 推理引擎选型 | ADR-001 |
| O-000 | 窗口截屏方案 | ADR-002 |
| O-000 | 语音管线 | ADR-003 |
| O-000 | 桌宠技术栈 | ADR-004 |
| O-000 | 推送插件 | ADR-005 |
| O-000 | 后台架构 | ADR-006 |
| O-000 | 宠物视觉 | ADR-007 |
| O-000 | 监控策略 | ADR-008 |
| O-010 | HomeRail 开源项目评估 | ADR-009 |

### O-011 混合大脑架构（2026-08-03 用户裁决）
- 类别：technical
- 描述：任务拆解/评审/指令生成用哪个模型
- 决策：✅ **本地 9B（MiniCPM-o，监控/视觉/轻量）+ DeepSeek V4 Flash 正式版 API（拆解/评审/指令生成）混合**——用户要求"省钱到极致"；隐私：仅会话摘要上传云端，截图不出本机（延续 O-006）
- Resolves when：✅ 已裁决，V1.5 落地（DeepSeek 客户端 + 混合路由）

### O-012 指令注入方式（2026-08-03 待补裁）
- 类别：product-scope
- 描述：贾克斯生成的指令如何进入 Codex（全自动键鼠注入 / 确认后注入 / 仅生成文本）
- 决策：默认"**确认后注入**"（生成→用户确认→自动注入 Codex 输入框）起步，全自动留 V2——待用户最终确认
- Resolves when：V1.5 开发排期前用户确认

### O-013 安全边界重定义：受控注入（2026-08-03 用户裁决）
- 类别：product-scope
- 描述：原 PRD "只监控+提醒+建议，不操控" 升级
- 决策：✅ **受控注入**——注入前用户确认；注入内容仅指令文本（不读 Codex 内部数据/不键鼠模拟 UI 之外的操控）；截屏不出本机；上传云端仅会话摘要且脱敏
- Resolves when：✅ 已裁决；PM 同步更新 PRD §5.4


### O-014 手机语音对话（类 Siri/GPT-Live，2026-08-03 用户核心需求）
- 类别：product-scope
- 描述：用户核心诉求 = 手机跟贾克斯语音对话（唤醒→说话→语音回答），对标三星 Bixby/苹果 Siri/GPT-Live 体验。文字推送只是辅助，语音双向交互是主形态
- 近期路径（V1.5 增强）：**飞书机器人**——手机飞书发语音消息 → 事件订阅转发电脑贾克斯 → 本地 ASR（模型原生/sherpa-onnx）→ DeepSeek 大脑处理 → TTS 生成语音 → 上传回传飞书语音消息。**飞书即手机端 UI，无需自研 App**
- 远期路径（V2）：自研手机端（小程序/App）+ WebSocket 云端中继 → 实时流式语音 + 打断（完整 GPT-Live）
- Resolves when：用户创建飞书自建应用（提供 App ID/Secret）后实施

### O-015 语音形态红线：最终=本地模型原生全双工（GPT-Live 级）（2026-08-05 用户裁决）
- 类别：product-scope
- 描述：用户三条硬性要求：①不要 ASR 假语音来回制 ②不要不能迭代的替补方案 ③最终必须达到真正 GPT-Live 效果（流式双向+随时打断），不是"我说一句他回一句"
- 裁决：✅ **M3 全双工 = 唯一终点**（本地 llama-omni 原生 APM：流式 ASR+流式 TTS+实时打断 barge-in，mobile-voice-spec §8 apm_bridge + §4.4）；当前半双工（sherpa STT→大脑→edge-tts）仅为 M2 过渡调试链路，**不作为最终交付形态**；任何"不能迭代的替补"直接否决
- 落地路径：PoC B3（本地模型原生全双工 APM 验证）→ apm_bridge 实装 → App 端 barge-in（silero-vad 双门限）→ 全双工替换半双工
- Resolves when：M3 交付验收
