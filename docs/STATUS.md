# STATUS — 贾克斯模式项目状态快照（不依赖对话上下文的恢复点）

> 用途：任何新会话/新专家**只读本文件 + docs/decisions/ + docs/specs/ 即可恢复全部上下文**。
> 维护：每次重大决策/里程碑后由总监更新本文件（只追加新节，不删历史）。

---

## 1. 项目是什么（一句话）

Windows 桌面"贾克斯"：**屏幕级监控 + 混合大脑 + 多 harness 管家 + 远程指挥**——实时判断 Codex/Trae/Hermes/WorkBuddy 是否跑偏，把用户意图转化成最有效指令拆解并（确认后）注入，管理多 agent 并行任务；用户不在电脑前也能安排/验证/测试/QA。

## 2. 核心决策（2026-08-03 用户裁决，详见 OPEN-DECISIONS）

| 决策 | 结论 |
|---|---|
| 核心痛点 | 用户"指令能力有限" → 贾克斯价值 = **意图→最优指令拆解**（非单纯监控提醒） |
| 混合大脑 | 本地 9B（监控/视觉）+ **DeepSeek V4 Flash API**（拆解/评审/指令生成），省钱到极致；仅会话摘要上传、截图不出本机 |
| 指令注入 | 默认"**确认后注入**"（生成→用户确认→自动注入 Codex 输入框），全自动留 V2 |
| 安全边界 | 从"只监控不操控"升级为"**受控注入**"（注入前确认、仅指令文本、截屏不出本机） |
| 远程指挥 | V1.5 单向（电脑→手机报告）；V2 双向（手机→电脑，云端中继加密） |
| 监控目标 | 4 个：Codex（桌面版重点，主窗口实为 **ChatGPT.exe**）/ Trae（**TRAE SOLO CN.exe**）/ Hermes / WorkBuddy |
| HomeRail | ADR-009 方案 B：**不引依赖，吸收 5 项设计**（配置分层/留痕回放/技能发现/可观测/语音契约） |
| 推送 | 企业微信 webhook + 脱敏（不附截图/不含代码）——webhook URL 待用户提供 |

## 3. 当前进度（2026-08-03 17:55）

| 阶段 | 状态 | 证据 |
|---|---|---|
| 全面审计（7 专家） | ✅ 完成 | docs/AUDIT-2026-08-03.md（10 P0 清单） |
| M-1 修复基线 | ✅ 7/7 完成 | 后端 67 测试绿 / 前端 build exit 0 / QA 变异 4/4 杀 / 5 份契约 docs/specs/ |
| PoC B0 模型 | ✅ 就绪 | D:\models 8.5GB 全齐（Q4_K_M + vision + audio + tts + token2wav） |
| PoC B1 视觉压测 | ✅ 通过（B 计划降 ctx 4096） | POC-001 报告：显存 9198MB/首token 439ms/端到端 1344ms/JSON 24/24；**SSE 格式确认：decode 返回 text/event-stream，llama_omni_client 须改 SSE 解析**（见 backend-llama-client-spec） |
| PoC B2 WGC 捕获 | ✅ 通过（3+1 窗口全过） | POC-002 报告：ChatGPT/Hermes/WorkBuddy/Trae 60 帧 100%；Trae 专项补测完成，不黑屏；**发现 Trae 最小化必崩 WGC**（见保留项，orchestrator 需最小化主动停 WGC + DXGI 兜底 + 重建） |
| PoC B3 语音 | ⏳ 排队 | 待 B1 契约回填后启动 |
| PRD 升级（大脑+管家） | 🔄 PM 进行中 | pm-v2 已 spawn（需求：意图拆解/混合大脑/受控注入/版本路线 V1→V1.5→V2） |
| V1 开发 | ⏳ 待 PoC 全过后启动 | 四流并行：后端监控/检测提醒/桌宠 UI/推送插件 |

## 4. 版本路线（2026-08-03 重构）

- **V1** 监控闭环：WGC 四目标截屏 → 视觉判定（progress/stuck/off_track）→ 四级渐进打扰 → 桌宠 + 推送（企微脱敏）
- **V1.5 大脑闭环**：意图理解 → 任务拆解 → 指令生成（DeepSeek）→ 用户确认 → 注入 Codex；单向报告（电脑→手机）
- **V2 管家**：多 harness 并行编排（任务队列/依赖）+ 双向远程指挥（手机→电脑，云端中继加密）+ 全自动注入

## 5. 关键环境事实

- venv：C:\Users\Administrator\WorkBuddy\监视app\.venv（Py3.11.9，62 依赖）｜managed python: C:\Users\Administrator\.workbuddy\binaries\python\envs\monitor-app
- 模型服务：llama-server.exe :19080（Q4_K_M + vision）；**B1 实测锁定启动参数 --ctx-size 4096**（8192 显存 12GB 超限）；Comni 未装（命令行 server 替代）
- 正确调用序列：omni_init → update_session_config → prefill(img+text) → decode(stream SSE)；**use_tts 必须 false**（否则 TTS 拖慢首 token 至 44s）
- windows-capture 2.0.0 已装可用；rust 工具链缺失（tauri build 待装 rust）
- git 基线：2c7660a + 5f1b41f（M-1 前基线）；.venv_old 待用户手动删
- 代理：127.0.0.1:7890（曾致 spawn 失败，恢复后正常）

## 6. Harness 自升级（ADR-010，2026-08-03）

六风险对策详见 docs/decisions/ADR-010-harness-hardening.md：
跑偏→Spec 锁定+门禁；失败→B 计划+3 次升级；翻车→git 基线+回滚；裸奔→QA 门禁+硬件路径实测；文件堆积→tmp 帧清理规则（WGC 帧文件上限）；越跑越笨→pitfalls.jsonl 踩坑自学习+STATUS.md 恢复点。

## 7. 待办（下一步）

1. ~~PoC B1 数据回传~~ ✅ 已完成 → **回填 backend-llama-client-spec.md §1/§3/§4 的 {{POC-B1}} 占位**（init=omni_init / prefill 两次 / decode SSE）→ 改 llama_omni_client.py
2. Trae 补测（代理恢复后 spawn）
3. PM 回传 PRD 升级 → 架构师更新架构（DeepSeek 客户端/混合路由/注入机制）→ 用户确认 V1.5 范围
4. V1 四流开发（PoC 全过后）
5. 用户提供企微 webhook URL
6. 用户确认 O-012 注入方式
7. 用户安装 rust（解锁 tauri build）
