# Hermes 委派闭环 QA 设计草案

状态：Phase 2 设计审查（测试先行，测试作者与实现作者解耦）  
依据：`requirements.md`（FR-1..FR-12、BR-1..BR-5、NFR-1..NFR-4、A1..A14）、ADR-023。  
目标档：Silver（商业生产最低线）；涉及高风险确认、密钥、不可逆动作的路径必须 Gold 级证据。

> 本文只定义验收测试、机械证据和放行门，不实现生产代码，不把静态检查或 EventBus mock 冒充端到端联通。实现前先锁定“必须锁定的契约”一节；真实 Hermes 未经现场探针前，任何 A2A 已联通结论均为无效。

## 1. 改动影响分析（基线）

### 本次设计/预期改动范围
- `backend/app/brain/schemas.py`：BrainTask 状态集合、`delegate_backend`、`remote_task_id`、版本/事件关联字段。
- `backend/app/brain/pipeline.py`：从 Injector 依赖改为 `AgentBackend`，路由、风险门禁、自动派发、恢复状态机。
- `backend/app/brain/store.py`：原子写、旧 JSON 向后兼容、幂等事件记录、重启恢复。
- `backend/app/core/events.py`、`backend/app/api/routes_ws.py`：统一任务事件、WS 广播、断线后的积压/一次性结果消费。
- 新增 Hermes A2A adapter、Agent Card/schema 契约、signed webhook handler、配置和审计。
- `backend/app/voice/apm_bridge.py`、Android `VoiceUiModel.kt` 及 sidecar：语音结果/重连/重启只影响输送，不改变后台事实。

### 下游影响面与风险
- 直接调用方：brain API 路由、Injector/ClipboardInjector、EventBus、WS Hub、语音 `on_text`/TTS、手机 UI 状态聚合。高风险。
- 共享状态：`brain_tasks.json`、事件去重表/审计日志、WS 待播报队列、Hermes 远端任务关联。高风险。
- 旧行为风险：默认 `codex_gui` 路径、旧 BrainTask JSON 读取、现有 `intent_ready → awaiting_confirm → injected` 行为。高风险。
- 外部契约风险：A2A Agent Card、请求 schema、webhook 原文 HMAC、远端状态/事件 ID。高风险。
- 连接恢复风险：backend/Hermes/App/sidecar/WS 任一重启或迟到/重复事件。高风险。

### 回归测试优先级
1. 高：高风险零 Hermes 调用、低风险自动派发一次、任务状态不可回退、重复 webhook 不重复副作用。
2. 高：旧 `codex_gui` 默认路径及旧 JSON 兼容；HMAC fail-closed 和隐私脱敏。
3. 中：WS 断线补发、手机/sidecar/后台重启恢复、取消语义。
4. 低：文案/进度格式等非契约视觉细节（仍需有可消费摘要）。

## 2. 追踪矩阵（FR → 测试层级 → 机械证据 → 失败判定）

| FR | 单元/属性与状态机 | 集成/契约/真实 HTTP | E2E/恢复 | 机械证据与失败判定 |
|---|---|---|---|---|
| FR-1 路由 | 表驱动 direct/delegate/clarify/deny；置信度低、缺参、重复输入 property；断言唯一 task_id/追问/拒绝原因 | fake `AgentBackend` 记录调用数=0/1；API schema 校验 | A1、A3：输入→brain→可见响应 | `route_decision`、task store、fake calls、事件 JSON。direct/clarify/deny 有任何 Hermes call、delegate 无唯一 task_id、重复副本或缺追问即失败/P0。 |
| FR-2 低风险自动派发 | 低风险白名单×置信度阈值×必填参数组合；保存先于 delegate 的顺序 property | fake backend + 故障注入（store 写失败、delegate timeout）；HTTP fake A2A | A2；受理与派发间杀 backend 后恢复 | store 时间戳/顺序日志、fake call、accepted+queued/running 事件。需要按钮/人工点击、未先落草稿、静默丢任务或重复远端创建即失败/P0。 |
| FR-3 高风险门禁 | 状态机 property：未确认/拒绝/过期/内容或版本变化/错误 token 均 call=0；明确确认只允许当前版本一次 | fake backend 断言确认前没有请求；确认后仅一次 | A4/A5/A6：语音提示含动作/目标/风险，确认后执行 | `confirm_audit`、token/version、fake calls。任何未确认调用= P0；旧 token 通过、提示缺动作/目标/风险=阻断。 |
| FR-4 AgentBackend | Protocol 类型检查；pipeline 对具体实现 import 的静态依赖检查；相同状态机替换 fake/codex/hermes | fake backend contract：`ok/channel/remote_task_id/error_class/retryable`；codex 回归 | 默认 `codex_gui` 旧流不变；切换配置不改路由 | 类型/依赖扫描、结果 schema、旧 API 测试。pipeline 直接耦合 Hermes/Codex、返回字段缺失或默认旧行为改变=阻断。 |
| FR-5 发现与鉴权 | discovery 结果分类（未启动/401/协议不兼容/timeout/network）；secret 不出 model/log | fake Agent Card 正/错版本、token 正/错；真实探针另行门禁 | 真实 Hermes 进程健康+Agent Card+鉴权证据；未验证不得标已派发 | HTTP transcript（去 secret）、Card schema、进程/health 证据。discovery 失败却 accepted/running、secret 入库/日志、真实探针缺失= P0。 |
| FR-6 隐私消息 | sanitizer property：原始语音、token/secret、路径/凭证不在 outbound；允许字段白名单 | fake A2A 捕获原始 bytes/JSON 和日志；schema 必须拒绝额外敏感字段 | A14：敏感输入→fake/真实 HTTP 捕获确认未泄露 | 捕获请求原文、脱敏审计日志、secret grep。任一 Hermes outbound/log 含原始敏感字段或 token= P0。 |
| FR-7 持久化 | 合法迁移全覆盖；非法迁移保持原状态；终态不可回退；事件幂等 property；旧 JSON 缺字段兼容 | 临时文件崩溃/损坏、原子替换和重载测试 | backend 重启恢复未决任务；A10 | 重启前后 JSON/hash、状态迁移拒绝记录、`updated_at`/终态时间。半文件丢任务、终态回 running、旧 JSON 读失败= P0。 |
| FR-8 webhook | HMAC 用原始 body、常量时间比较；缺签名/错签名/过期/未来偏移/重放/未知 remote/event 重复；事件 schema property | fake HTTP POST：body 字节重排、重复/乱序 session_activity/tool_completion/turn_completion | A8/A9 端到端 webhook→store→WS/TTS；不能只 mock EventBus | HTTP 状态码、原文 hash、事件去重键、store diff、播报计数。验证前更新状态、重放接受、未知任务更新、重复副作用= P0。 |
| FR-9 回报事件 | 统一事件 schema 必填 task/session/status/time/summary；摘要长度/脱敏 property；终态一次性消费 | EventBus→真实 WS Hub integration（实际 websocket client）；断连队列 | A11：会话结束后结果入队，下一次会话只播报一次；UI 不混淆 TRTC/任务 | WS frames、queue before/after、TTS callback count、UI model snapshot。缺事件/secret 大段输出/重复播报/用连接态冒充任务态=阻断。 |
| FR-10 失败超时恢复 | retryable 分类与指数退避有上限；不可重试错误零重试；状态 property | fake A2A/HTTP 断连、迟到 webhook、超时和协议错 | backend/Hermes/App/sidecar/WS 各自重启；远端 ID 不重复创建；A7/A10/A11/A12 | retry trace、进程重启命令/时间线、remote ID 集合。无限重试、伪报完成、重启重建 remote task、回报输送故障改变事实= P0。 |
| FR-11 取消/人工接管 | 归属+状态矩阵；completed/不可逆执行不可伪取消；取消幂等 | fake backend cancel 正/不支持/timeout；审计 | A13：取消事实与不可取消提示可见 | cancel request、状态/审计事件。越权取消、完成态伪取消、无独立审计或“不支持取消”伪成功=阻断。 |
| FR-12 观测审计 | 事件时间线 schema、来源分类、脱敏 property；审计失败时 high fail-closed、low 显式降级 | fake store/log sink 故障；按 task_id 重建完整 timeline | A2/A4/A7/A8/A10/A13/A14 取样审计 | 结构化审计 JSON、关联 ID、耗时/重试/error class。无法重建、密钥泄露、高风险因审计失败绕过确认= P0。 |

### 测试层级定义
- **Unit**：纯函数、schema、路由、脱敏、签名、错误分类；无网络、无真实时钟（注入 clock）。
- **State/property**：模型化状态机随机生成合法/非法迁移、重复/乱序事件，断言不变量。
- **Integration**：fake AgentBackend、临时真实 `TaskStore`、真实 EventBus/WS Hub、HTTP fake server；不能把所有边界 mock 掉。
- **Contract**：A2A Agent Card、A2A request/response、webhook event schema 和错误模型双向校验。
- **E2E/recovery**：真实 HTTP socket、真实 backend 进程、实际 WS client、实际 App/sidecar/Hermes 进程重启；保留命令、PID、health、请求/回报 transcript。

## 3. 核心用例目录（Given/When/Then）

### 3.1 路由、风险、确认

| ID | Given / When | Then（硬断言） |
|---|---|---|
| QA-R1 | 输入知识问答 | route=direct_reply；task store 无可执行任务；AgentBackend calls=0。 |
| QA-R2 | 输入“查失败测试并总结”，低风险、阈值达标、参数完整 | 先写脱敏草稿，再一次 delegate；accepted 与 queued/running 均有 task/session。 |
| QA-R3 | 低风险但缺目标/范围 | clarify，至少一个可回答问题；calls=0。 |
| QA-R4 | 删除/外发/支付/凭证/未知命令 | high 或 blocked；未确认 calls=0，任务为 awaiting_confirm/denied。 |
| QA-R5 | 确认“确认执行”且 token、task version 匹配 | 仅当前 task 版本一次 delegate；确认审计含来源与时间。 |
| QA-R6 | “嗯/好的/继续”上下文不唯一、拒绝、超时、内容变更 | 追问或 denied/expired；calls=0；旧 token/version 永不复用。 |
| QA-R7 | 同一输入并发/重复提交 | 最多一个有效 remote association；幂等返回或可关联副本，不重复副作用。 |

### 3.2 A2A、webhook、安全

| ID | Given / When | Then |
|---|---|---|
| QA-A2A1 | fake Agent Card 合法 | discovery 记录能力、协议版本、鉴权；随后 request schema 合法。 |
| QA-A2A2 | Card 不存在/401/版本错/timeout | 分类失败；不得发 accepted/running，不无限重试。 |
| QA-H1 | 合法原始 body + 正确 HMAC + 当前 timestamp | 200/受理，按 remote_task_id 更新一次。 |
| QA-H2 | 缺 header、错误签名、body 重排后旧签名、过期/未来时间 | fail-closed（4xx）；状态/事件/播报均不变。 |
| QA-H3 | 已消费 event_id、未知 remote_task_id、乱序终态后进度 | 重复返回幂等且无二次副作用；未知 ID 拒绝；终态不可回退。 |
| QA-H4 | session_activity→tool_completion→turn_completion | 进度、工具结果、终态均可映射；按 task_id 重建顺序/时间线。 |
| QA-S1 | 输入含 API key、secret、完整路径、原始语音片段 | outbound 仅白名单脱敏摘要/约束；日志和错误响应不含秘密。 |

### 3.3 持久化、重启、回报

| ID | 操作 | Then |
|---|---|---|
| QA-P1 | 写状态过程中 kill backend | 重启后 JSON 可解析；任务为原状态或 recovering，不出现半 JSON/伪完成。 |
| QA-P2 | 用旧 `brain_tasks.json` 缺新增字段加载 | 兼容默认值，历史任务可列出且旧 codex 行为不变。 |
| QA-P3 | webhook 到达后重复 POST | 事件去重；状态、审计副作用、TTS/WS 发送计数均只增一次。 |
| QA-P4 | WS 断线期间任务完成，随后重连 | 任务事实持续更新；积压终态可取且只消费一次。 |
| QA-P5 | 手机 App 重启/sidecar 重启/TRTC 中断 | 后台任务事实不变；恢复连接可取最新状态和一次性结果。 |
| QA-P6 | Hermes 进程重启且已有 remote_task_id | discovery/status/webhook 恢复；remote task 创建请求集合仍只有一个。 |
| QA-P7 | backend、Hermes、App/sidecar 组合重启 | 最终状态可解释为 completed/failed/recovering/needs_input，不把旧任务播报为新任务。 |

## 4. AgentBackend fake 与真实 HTTP fake server 设计

### Fake AgentBackend（必须）
- 只暴露 `delegate(task)` /（如契约确定）`cancel(task)`；记录调用序列、task_id、幂等键、脱敏 payload、返回 `DelegateResult`。
- 可注入：成功、可重试 timeout、不可重试 401/协议错/权限错、重复响应、远端 ID 冲突。
- 机械断言：调用前 store 已有草稿；高风险确认前调用数严格为 0；同 task 的 delegate 次数≤1；返回字段完整且 `retryable` 与错误分类一致。

### 真实 HTTP fake server（必须，不能只 mock client）
- loopback TCP server 提供 Agent Card、A2A call/status、webhook sink；保留每次请求的 method/path/headers（secret 脱敏）/原始 bytes/时间。
- 在网络真实序列上注入连接拒绝、半响应、超时、响应乱序、重复 webhook、body 字节重排；客户端使用真实 HTTP 库。
- 机械证据：server request transcript、请求幂等键集合、webhook 原文 bytes hash、响应 code/latency、backend TaskStore 前后快照。
- “EventBus handler 被调用”只证明进程内分发，不证明 HTTP/A2A、Hermes webhook、手机/语音链路已打通；不得以此签署 FR-5/FR-8/FR-9/FR-10。

## 5. 真实 Hermes 现场探针门禁

### 未验证前只能用 fake server 的项目
- A2A Agent Card URL/字段、实际请求 envelope、auth header 语义、HTTP status/error 模型。
- `session_activity`、`tool_completion`、`turn_completion` 的真实 payload、稳定 `event_id`、`remote_task_id`、取消能力和进度字段。
- 长任务 webhook 时序、Hermes 重启后的 status/discovery 事实恢复、真实 300s/round-trip 行为。
- 因此 fake server 通过不得写成“Hermes 已联通”，只能标“adapter 行为通过受控协议替身”。

### 现场探针最小放行门（每项留证，缺一即 fail）
1. **进程**：实际 Hermes 进程名、版本、PID、启动命令/工作目录、监听地址；确认独立进程（backend 不托管/不 fork）。
2. **健康**：真实 health/status endpoint 返回成功；记录时间、HTTP code、响应 schema（敏感字段脱敏）。
3. **Agent Card**：真实 `/.well-known/agent.json`（或官方确认地址）返回；校验协议版本、能力、endpoint、鉴权要求。
4. **A2A 请求**：真实 token 调用一个低风险、无副作用探针任务；记录原始请求/响应 hash、task/context ID、无 secret transcript。
5. **webhook 回报**：真实 Hermes 向受控 backend webhook 发送 signed `session_activity`/`tool_completion`/`turn_completion`；保存原文 hash、HMAC 校验结果、event_id/remote_task_id。
6. **任务状态**：从创建到终态在 backend store、Hermes status（如有）、WS/语音摘要三侧可关联；task_id/session_id/remote_task_id 一致。
7. **重复事件**：同一 webhook 原文重发至少一次；状态、审计和语音播报各只生效一次。探针报告含进程、health、Card、A2A、webhook、状态、重复事件七类证据。

真实联调还必须覆盖 Hermes kill/restart：已有 remote_task_id 不得重新创建；事实不确定时只允许 recovering/needs_input。

## 6. 状态机与 property 不变量

状态集合至少覆盖：`intent_ready, clarify, awaiting_confirm, queued, running, needs_input, completed, failed, cancelled, denied, expired, recovering`。实现若保留旧 `decomposed/injected`，须定义映射/兼容策略，不能产生两套事实。

不可违反的不变量：
1. `direct_reply/clarify/deny` 不产生 Hermes 可执行调用；`blocked` 永不调用。
2. high 风险在当前 task version 的明确确认前调用数=0；内容/目标变化或确认过期立即失效。
3. 一个 task_id 至多一个有效 remote_task_id；重试沿用幂等键，不能新建副作用。
4. 终态 `completed/failed/cancelled/denied/expired` 不被任何迟到/重复事件回退。
5. 所有状态变化原子落盘且有 `updated_at`；终态有 completed/failed 时间。
6. `event_id` 去重是持久化事实，不可仅存在内存；重复进程/重启后仍只生效一次。
7. 语音/WS/手机/sidecar 断线不改变任务事实；终态待播报消费至多一次。
8. 审计失败时 high fail-closed；low 必须显式记录 audit-unavailable 降级，不得静默。

## 7. 防假绿与测试完整性门禁

每次实现交付前后均保留基线并机器执行：

1. `git diff --name-status`：`tests/` 或 `*.test.*`/`*.spec.*` 被删即 P0；测试行数骤减需独立解释。
2. 对比前后 `expect(`/`assert` 数量；保留测试断言减少即 P0，除非有独立评审批准且区分力不降。
3. 对比新增 `skip/xfail/.only/focus/@pytest.mark.skip/ignore`；新增即默认 P0 阻断，须登记真实缺陷与期限才能复核。
4. 分离实现 diff 与测试/fixture/门禁 diff；人工审查期望值来自 requirements/契约，不是复制实现输出；禁止测试输入特判、吞异常、提前 return。
5. 检查 `jest.config.*`、`pytest.ini`、`pyproject.toml`、`package.json` 的 test script、coverage 阈值和必过检查；降低阈值/新增豁免即 P0。
6. 必须有作者不可见 held-out 输入（路由边界、HMAC 原文变体、重复事件、确认歧义）和独立确定性 runner；可见用例全绿不能替代。
7. 核心路径做定向变异：把 `<`/`<=`、确认判断取反、HMAC 改解析后 JSON、终态允许回退、去重表改内存等缺陷注入；对应杀手测试必须变红。
8. 所有“启动/进程/跑通”必须有 PID/命令、health HTTP、实际 socket transcript、store/WS/TTS 结果；静态 grep、mock EventBus、截图均不足以证明闭环。

## 8. P0 阻断项（任一存在即 no-go）

- 高风险未确认/拒绝/超时/版本变化仍调用 Hermes（零调用门禁失败）。
- 真实或 fake webhook 在 HMAC 原文、时钟窗口、重放、未知任务任一 fail-closed 失败。
- 重复 webhook/重试/重启造成重复远端任务、重复不可逆副作用或重复语音播报。
- 终态回退、任务丢失、半 JSON、重启伪报完成、无法区分 recovering/needs_input。
- 原始语音、token、secret、凭证、完整敏感路径进入 Hermes 请求、日志、审计或错误响应。
- 仅 mock EventBus/HTTP client 却宣称手机/sidecar/语音回报已打通；缺真实 HTTP/WS/进程证据。
- 真实 Hermes endpoint 未做七项现场探针，却标记 A2A 已联通或上线。
- `docs/openapi.yaml` 缺少实际 brain/webhook/cancel paths，或端点缺逐项 request/response、认证、错误 schema。
- `dispatch_attempt`/`retry_count`/`next_retry_at` 仅在恢复文档出现而未进入 BrainTask、持久化、事件和 API 响应；取消 `requested/confirmed` 与 report_queue 字段/ack 语义不一致。
- 旧 BrainTask JSON/默认 codex_gui 回归破坏；pipeline 直接依赖具体后端，抽象契约不可替换。
- 测试文件/断言被删或弱化，新增 skip/xfail/.only，测试框架/覆盖率门禁被放宽，或实现特判可见测试输入。
- 前端代码扫描到 emoji 功能图标（P0）；紫→粉渐变或 `Welcome to`/`Lorem ipsum` 等属 P1，不能伪装为通过。

## 9. 实现前必须锁定的契约（否则测试无法判定）

1. **路由/风险契约**：低风险白名单、置信度阈值、缺参规则、blocked 与 high 的边界；风险不确定一律 high/clarify。
2. **状态机契约**：最终状态枚举（旧 `decomposed/injected` 如何映射）、所有允许迁移、版本递增点、终态集合和错误分类。
3. **AgentBackend 契约**：delegate/cancel 签名、幂等键位置、`DelegateResult` 的 `ok/channel/remote_task_id/error_class/retryable` 必填性。
4. **A2A 契约**：采用的 A2A v1.0 envelope、Card URL/字段、鉴权 header、超时/round-trip、成功与错误 schema。
5. **webhook 契约**：签名构造（timestamp + 原文的拼接规则）、header 名、时钟窗口、event_id、remote_task_id、事件 payload 和 HTTP 响应。
6. **持久化契约**：原子替换策略、旧 JSON 默认值、去重记录存储、字段脱敏、并发写冲突策略。
7. **事件/回报契约**：统一事件名、必填字段、摘要长度/语言、WS 断线积压与一次性消费、语音结果优先级/播报策略。
8. **重启契约**：backend/Hermes/App/sidecar 任一重启后的状态判定、远端 status 能力、无法确认事实时的 recovering/needs_input。
9. **取消/审计契约**：取消支持矩阵、人工接管入口、审计事件来源分类、审计故障降级策略。
10. **现场运行契约**：Hermes 版本、真实进程名/启动方式、运行目录、WSL2 地址、health/Card URL、token/secret 来源（仅受保护配置）。
11. **HTTP/OpenAPI 契约**：`docs/openapi.yaml` 必须列出 brain intent/decompose/confirm/list、webhook 及取消等实际 paths；每个端点必须有独立 request/response schema、认证要求、状态码和统一错误模型。只有泛化请求 JSON 或仅端点表不能作为契约证据。
12. **恢复/重试字段契约**：`dispatch_attempt`、`retry_count`、`next_retry_at`（以及恢复原因/最后错误分类）必须在 BrainTask schema、持久化样例、状态事件和 API response 中形状一致；缺字段或只在文档出现均阻断。
13. **取消契约**：取消请求、`requested`/`confirmed`（或等价状态）字段、取消 API response、不可取消原因、审计事件和幂等语义必须逐项定义；不能只写业务描述。
14. **report_queue 契约**：待播报队列的字段、入队/取出/ack/去重及与 WS/语音事件的映射必须和交互稿及 API schema 一致；字段名或消费语义不一致即阻断。

## 10. 生产就绪记分卡（初始裁决）

| 维度 | 当前 Phase 2 目标/证据门 | 当前档位（未实现前） |
|---|---|---|
| 测试+回归 | 本文矩阵、held-out、定向变异、回归率=0 | Bronze（设计已定义，尚无实现证据） |
| 契约 | 需锁定 AgentBackend/A2A/webhook/状态 schema；真实 Card 探针 | Bronze |
| 安全 | HMAC 原文/常量时间/窗口/重放、脱敏、high fail-closed | Bronze（门禁定义，待执行） |
| 无障碍 | 语音提示明确且 UI 状态可消费；Android 状态契约回归 | Bronze |
| 性能 | A2A timeout/重试上限、事件处理不拖垮总线；尚无 p95/容量证据 | Bronze |
| 可观测 | task timeline、审计来源、耗时/重试/关联 ID；尚无运行证据 | Bronze |
| 发布安全 | codex_gui 回滚、Hermes 开关、独立进程/探针和回滚演练 | Bronze |
| **总档（取最低）** | **未达到 Silver；不得商业生产** | **Bronze / no-go** |

## 11. 放行判定与报告要求

- 冒烟 A1→A2→A4（未确认零调用→确认执行）→A9 重复事件→A10 重启恢复必须全绿；任一失败直接 no-go。
- 功能测试通过率目标 100%；P0=0；回归率=0；解决率=100%；无新增反作弊信号。
- 真实 Hermes 七项探针未完成时，报告必须明确“fake server 通过、真实 endpoint 未验证”，不能写“联通”。
- RoleVerdict 仅允许 `pass`/`fail`，blocking 每项写违反契约、证据、期望；advisory 不阻断；证据包含 artifact_ref 与行号/命令输出。
- 任一 P0 修复后新增 `tests/regression/<缺陷名>.test.ts`（或项目对应语言）并进入每次门禁。

## 12. Phase 2 设计审查结论（针对当前代码基线）

当前读取到的基线仍是旧 brain 状态机与注入模型：`schemas.py` 仅有 `intent_ready/decomposed/awaiting_confirm/injected/denied/failed/expired`，`pipeline.py` 仍注入 `Injector`，`routes_ws.py` 仅订阅 session/alert/auth 事件，`TaskStore._save()` 直接写目标文件，且未见 Hermes webhook/A2A adapter。故本草案可作为实现验收门，但不能把当前代码标为满足 FR-1..FR-12；实现阶段必须先补齐契约并按上表逐层验证。
