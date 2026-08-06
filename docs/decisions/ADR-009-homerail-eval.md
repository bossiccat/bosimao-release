# ADR-009: HomeRail 开源项目评估 — 借鉴设计理念，不引入技术依赖

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远（经项目总监审计）
- 决策类型：design-evaluation（开源项目复用评估）

## 背景

项目总监提出重大架构决策候选：开源项目 **HomeRail**（github.com/xiaotianfotos/homerail，MIT，0.1.0-beta.1，2026-07-07 创建，226 commits，487 stars/111 forks）是"准成熟产品且支持二次改造"，倾向评估**基于 HomeRail 二次开发**而非自研，避免重复造轮子。

**HomeRail 已核实事实**（总监一手资料 + 本 ADR 联网复核一致）：
- 技术栈：TypeScript monorepo（protocol/manager/node/worker/cli/agent-ui/plugin_sdk）；依赖 Node 20+/Docker；Claude Agent SDK 兼容 endpoint；harness 目标为 codex_appserver / claude-sdk / kimi-code
- 能力：DAG 多 agent 编排（显式交接/重放/评分卡/eval-run）、语音面契约（ASR/TTS/VAD 中文默认 + 桌面语音壳）、生成式 UI（探索中）、agent-ui 浏览器面板（Vue 329/329 测试 + 设计令牌主题）、多层配置（config.json/profile/加密凭据）、插件 SDK + skills/ 技能发现、systemd 运维、WebSocket 控制面安全
- ROADMAP 明确：**不做软件开发自动化**（non-goal）；不构建 harness（集成 claude-sdk/codex_appserver/kimi-code）；目标是"结果易评估"任务（视频/报告/资产）；面向家庭数据中心常驻 agent（voice in, generated UI out）

**我们已自研资产**（沉没成本，勿忽略）：backend 67 测试绿（config/snapshot/DXGI 裁剪/降采样/PushManager/advice/trigger/monitors.yaml）、5 份组件契约 docs/specs/、前端 build exit 0（Tauri 桌宠 + 六态机 + 四级打扰）、WGC 授权设计、llama SSE 客户端设计、显存守卫设计；PoC B2 已过（三窗口 60 帧 100%）、B0 模型就绪、B1 压测进行中。

## 六维对比

| 维度 | 评估 | 结论 |
|---|---|---|
| **技术栈契合度** | 我们后端 = Python 3.11+ FastAPI（ADR-006 已裁决，因 windows-capture/silero-vad/sherpa-onnx 全 Python 库直接绑定）；HomeRail = TypeScript + Node 20+ + Docker。TS 恰好是 ADR-006 明确否决的路线（"Node.js 弱，需 child_process 调 CLI"） | **低契合** |
| **功能覆盖** | 见下表逐项 | **核心差异零覆盖** |
| **二次改造工作量** | 见下文"改造归属矩阵" | **核心全部自研，HomeRail 无可复用代码** |
| **生态与成熟度** | HomeRail 仅约 1 个月历史，0.1.0-beta.1，generative UI 契约"will keep changing"（官方自述）；我们 67 测试绿 + 5 契约已锁定 | **beta 依赖风险高** |
| **维护风险** | 引入 HomeRail = 同时承担上游 beta 演进 + Docker/Node 运行时 + 双语言桥接 + 我们核心自研，风险叠加 | **高风险** |
| **迁移成本** | A 方案 = 67 测试/5 契约作废 + 重写后端 + 学习其 DAG/skill 体系，2 周不可行 | **高成本** |

### 功能覆盖逐项（我们的需求 vs HomeRail 能力）

| 我们的需求 | HomeRail | 结论 |
|---|---|---|
| WGC 屏幕监控（4 目标窗口） | 无任何截屏/窗口/视觉能力 | 必须自研 |
| 本地 omni 多模态判定（progress/stuck/off_track） | 无；依赖云 harness（claude-sdk/codex_appserver/kimi-code）执行代码任务 | 必须自研 |
| 桌宠 UI（六态机/四级打扰/光球） | 无；仅浏览器 agent-ui 面板（Vue）+ generative UI 探索中 | 必须自研（ADR-004 Tauri 已定） |
| 手机推送（企微/ntfy） | 无 | 必须自研 |
| 持续监控循环 → 事件判定 → 提醒 | 无；执行模型为"请求→DAG→可评估结果"批处理 | 必须自研 |
| 语音管线（V1.1 全双工） | **有**：语音面契约（ASR/TTS/VAD 中文默认 + 桌面语音壳） | **理念重叠，可借鉴**（我们实现走 llama.cpp-omni 原生 APM/TTS，不引依赖） |
| 配置分层（config.json/profile/加密凭据） | **有**：多层配置 + 加密凭据 | **可借鉴设计**（低成本落地） |
| 可观测/判定留痕（replay/scorecard/eval-run） | **有**：运行可重放、评分卡、run evaluation | **可借鉴设计**（ADR-008 判定留痕已有基础） |
| 技能发现（skills/ + 插件 SDK） | **有**：plugin_sdk + skills/ 技能发现 | **可借鉴设计**（V1.2 受益） |
| WebSocket 控制面安全 | **有**：token/反向代理/证书 | 参考（我们已有 WS 心跳/退避，安全面小） |

## 关键问题回答

**Q1. 我们的核心差异（WGC 监控 + 本地 omni 判定 + 桌宠提醒）HomeRail 是否有现成支持？**
没有。三项全部零覆盖。HomeRail 的世界模型是"任务队列"（一次请求 → DAG 执行 → 可评估结果），我们的世界模型是"常驻守护"（持续监控 → 事件判定 → 渐进打扰）。产品价值面完全不重叠。

**Q2. "持续监控→事件判定"与它"请求→DAG"模式能否融合？**
- 理念层：可融合——两者共享"显式状态、可观测、留痕可回放、注意力稀缺→打扰最小化"哲学，互相印证。
- 架构层：不可直接融合。HomeRail DAG 是**执行期一次性拓扑**（run 生命周期、per-run workspace、replay），我们的监控循环是**常驻无限循环**（tick 生命周期、显存时分、5-8s 低延迟判定）。把监控写成 DAG node 会引发 run 生命周期不匹配、低延迟不满足、显存守卫难以表达（ADR-001 单实例硬约束）等结构性摩擦——"用批处理框架做实时守护"是反模式。

**Q3. TS 后端能否调用我们已验证的 Python 捕获层（跨语言方案）？**
技术上可行（child_process / FFI / HTTP 服务化），但这是 ADR-006 明确否决的架构（Node.js 需 child_process 调 CLI → 否决），且引入双语言桥接成本：WGC 授权交互、帧流传输、进程编排、错误传播全需跨语言重做，测试矩阵翻倍，2 周内不可行。

## 决策

**推荐方案 B：混合——保留 Python 监控后端，借鉴 HomeRail 的设计理念（配置分层/技能发现/可观测/语音面契约），不引入 HomeRail 技术依赖。**

否决方案：
- **A. 完全基于 HomeRail 二次开发（抛弃 Python 后端重写 TS）**：否决。核心价值面 HomeRail 零覆盖，等于抛弃已验证资产（67 测试/5 契约/ADR-001~008）去重写一个"我们不需要的 DAG 编排器 + 一个仍要自研的监控后端"；且推翻 ADR-006 无新证据（HomeRail 的 TS 栈不改变 windows-capture/silero-vad 只绑 Python 的事实）。
- **C. 维持自研、零吸收**：否决。会错过配置分层与可观测设计的低成本收益（约 3-5 人日落地）。
- **B 是更优解**：零技术依赖风险（不引入 beta 依赖 + Docker + Node 20+），保留 2 周可交付 V1 的节奏，同时吸收 HomeRail 真正有价值的"设计理念"而非"代码"。

## 必须吸收的设计（V1 落地前）

1. **配置分层模型**：现 config/（monitors.yaml/detection.yaml/push 等）升级为"基础配置 + profile 覆盖 + 加密凭据分离"三层；webhook/ntfy topic 等敏感项移入加密凭据层，不落明文（对齐 O-006 隐私边界）。
2. **判定留痕可回放**：ADR-008"每次检测结果落盘"升级为结构化事件日志（时间/窗口/状态/摘要/建议/模型耗时），提供回放工具，供回归集与误判审计——对齐 HomeRail replay/scorecard 理念。
3. **技能发现（skills 目录契约，轻量）**：建议生成器/报告生成器按"技能清单 + 输入输出契约"组织，V1.2 受益，成本低。
4. **可观测体系增强**：/health、inference_busy（backend-llama-client-spec §5 已接线）基础上，补结构化日志 + 状态快照，V1 可审计。
5. **语音面契约对照**（V1.1 语音管线设计时）：对照 HomeRail Voice Surface Contract 的 ASR/TTS/VAD 契约化思想设计我们的语音模块（O-010 已登记，本 ADR 正式化）。实现仍走 llama.cpp-omni 原生 APM/TTS（ADR-001），不引 HomeRail 依赖。

## 后果

- 正面：
  - 核心链路（WGC + omni 判定 + 桌宠 + 推送）保持已验证的 Python 栈，67 测试/5 契约/ADR-001~008 全部保值
  - 吸收 HomeRail 设计理念的 5 项落地成本低（约 3-5 人日），V1 节奏不受影响
  - 不引入 beta 依赖 + Docker + Node 20+，维护面单一（纯 Python + Tauri React）
- 负面：
  - 不获得 HomeRail 的 DAG 编排/语音壳/agent-ui 面板代码（但我们当前阶段不需要）
  - 未来若需"多任务编排/重放评分"能力，需在 Python 栈内自建（评估：非 V1/V1.1 范围，V1.2+ 再议）
- 替代触发条件：V1.2 出现强"任务编排 + 评分卡"需求且自建成本 > 4 周 → 重新评估基于 HomeRail 的旁路集成（保留 Python 监控后端，HomeRail 仅作任务层），届时以新 ADR 更新本决策

## 相关 ADR

- ADR-001（本地推理引擎 llama.cpp-omni）、ADR-006（后台架构 Python FastAPI，Node.js 否决）、ADR-002（WGC 捕获）、ADR-004（桌宠 Tauri）、ADR-008（监控策略/判定留痕）
- O-010（HomeRail 参考评估，本 ADR 升格解决）
