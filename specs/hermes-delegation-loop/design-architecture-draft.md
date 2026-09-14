# Hermes 委派闭环架构设计稿

- **文档状态**：Phase 2 技术设计草案，供项目总监裁决后进入实现
- **日期**：2026-08-22
- **适用范围**：波斯猫语音前台 → backend brain → Hermes 独立进程 → A2A over HTTP → 状态/结果回报
- **权威需求**：`specs/hermes-delegation-loop/requirements.md`
- **已接受决策**：`docs/decisions/ADR-023-hermes-integration-boundary.md`
- **本稿原则**：以现有代码事实为准；Hermes 协议细节未在本机探针确认的部分全部标记为“待现场探针”，不得按占位字段直接实现。

## 1. 设计结论摘要

最终采用 **独立 Hermes 进程 + A2A over HTTP + signed webhook 回报**。backend 保留语音会话、R2 意图提取、R3 脱敏、风险门禁、任务事实、事件分发和审计；Hermes 仅作为黑盒后台执行引擎。`BrainPipeline` 只依赖 `AgentBackend` 抽象，不直接依赖 Hermes 或 Codex GUI。

默认后端仍为 `codex_gui`，因此在 Hermes 未通过现场协议探针、真实 HTTP 联调和回滚门禁前，现有剪贴板注入行为不变。Hermes 失败、未启动、鉴权失败、协议不兼容时均不得伪报“已派发”。

> 图标约束：如后续 UI 新增功能图标，统一采用 Lucide React（SVG）并锁定为项目唯一图标库；本架构不使用 emoji，不采用紫色到粉色渐变。

## 2. 当前架构事实（证据锚点）

### 2.1 语音与文本链路

1. `backend/app/voice/apm_bridge.py:66-77` 定义 `ApmBridge`，通过 `on_text` 回调输出模型文本；`251-295` 的接收循环将文本事件交给上层。
2. `backend/rtc_bridge/session.py:109-120` 将 `ApmBridge` 绑定到会话；`212-214` 的 `_on_text` 当前只记录日志，尚未接入 `BrainPipeline`。因此“语音文本→brain”的正式入口是 **待实现接线**，不是已验证事实。
3. `backend/app/api/routes_brain.py:18-61` 当前提供 `/api/v1/brain/intent`，支持 `text|voice`、`session_id`，创建意图任务；`64-103` 提供拆解、确认注入、任务列表。
4. `backend/app/core/events.py:38-55` 已有 `EventBus` 与 `EVT_BRAIN_*` 事件；`backend/app/api/routes_ws.py:24-86` 当前 `WsHub` 仅订阅会话、告警和授权事件，尚未订阅脑任务事件。

### 2.2 Brain 现状

- `backend/app/brain/schemas.py:12-14` 的 `TaskStatus` 当前仅有 `intent_ready/decomposed/awaiting_confirm/injected/denied/failed/expired`。
- `schemas.py:68-85` 的 `BrainTask` 已有 `task_id/status/intent/subtasks/instruction/review/created_at/updated_at/error/degraded/source/session_id/confirm_token`，没有远端任务 ID、后端 ID、事件去重集合、版本和进度字段。
- `backend/app/brain/pipeline.py:52-73` 的 `BrainPipeline` 接受具体 `Injector`；`136-191` 编排拆解；`193-257` 在确认后执行焦点校验、注入并把状态置为 `injected`。
- `backend/app/brain/injector.py:50-123` 的 `Injector` 将指令写剪贴板并通过 Win32 `SendInput` 注入 Codex GUI，文件兜底和审计均在本类中。
- `backend/app/brain/store.py:21-78` 使用内存字典 + JSON 文件；当前 `_save()` 直接 `write_text`，不是原子替换；损坏文件会 warning 后从空开始。

### 2.3 配置与进程装配

- `backend/app/config.py:132-157` 的 `InjectConfig/BrainConfig` 只有 Codex 注入参数，没有 delegate/Hermes 配置；`255-299` 负责 YAML 与环境加载。
- `backend/app/main.py:145-154` 在 lifespan 内实例化 `TaskStore`、`IntentService`、`TaskService`、`Injector` 和 `BrainPipeline`；这是后端实现 `AgentBackend` 选择的唯一装配点。
- `backend/app/api/routes_ws.py:65-86` 的 WS 路由运行在 lifespan 创建的 `EventBus` 上。

### 2.4 Hermes 事实分级

**ADR-023 已核验、可作为设计基线但仍需本项目现场复核的事实**：Hermes v0.20.0 声称包含 A2A plugin、Agent Card discovery、token、signed outbound webhook；ADR-023 第 27-47 行给出来源结论。该段不是本机联调证据。

**本项目已验证事实**：截至本稿编写，未有本仓库中的 Hermes A2A 请求、Agent Card 原文、webhook 原文、远端任务 ID 样例或取消响应样例。因此 endpoint、HTTP method、请求/响应 payload、签名头名称、事件 ID 字段名、取消接口均属于 **待现场探针**。本稿只定义 adapter boundary 和内部规范字段，禁止把 placeholder 当作官方字段。

## 3. 方案对比与最终裁决

评分 1–5，权重：学习/团队熟悉度 30%，生态与协议成熟度 25%，部署/运维成本 20%，隔离与隐私 15%，演进性 10%。

| 方案 | 形态 | 学习/熟悉度 | 生态/协议 | 成本 | 隔离隐私 | 演进 | 加权结论 |
|---|---|---:|---:|---:|---:|---:|---:|
| A（采用）| Hermes 独立进程，A2A HTTP，webhook 回报 | 5 | 5 | 4 | 5 | 5 | **4.8** |
| B | ACP stdio/JSON-RPC 接入 Hermes | 3 | 3 | 3 | 3 | 2 | **2.9** |
| C | 共享 backend 进程内存/内部 IPC | 4 | 1 | 5 | 2 | 1 | **2.8** |
| D | backend 宿主拉起 Hermes 子进程（stdio/HTTP） | 3 | 3 | 2 | 3 | 2 | **2.7** |

### 3.1 采用 A：独立进程 + A2A

- 符合 ADR-023 D1/D2/D3；跨 Windows/WSL2 只依赖 HTTP，不共享内存。
- A2A 的方向是 agent↔agent，符合“语音前台产出任务→后台 agent 执行”；ACP 是 editor↔agent，语义方向不匹配。
- Hermes 生命周期、memory.db、升级和工具引擎独立，波斯猫不 fork、不换皮、不自研通用 Agent 引擎。
- webhook 仅作为回报入口；长任务不可依赖一次性 HTTP 响应，进度事实以签名事件和可验证恢复流程为准。

### 3.2 不采用 B：ACP

ACP 是编辑器/客户端到 agent 的协议方向，且本项目跨独立进程与 WSL2 的核心需求是 HTTP 网络边界。即使 Hermes 存在 ACP adapter，也不能据此推导它提供 agent 委派语义。ACP 只保留为明确不采用项。

### 3.3 不采用 C：共享进程内存/内部 IPC

共享内存、Python import 或本地 pipe 会把故障域、升级、权限和生命周期绑定到 backend；无法自然跨 WSL2；还会让 Hermes 内部模型与工具实现渗入 BrainPipeline。内部 IPC 如需短期过渡，只能藏在 `AgentBackend` 后，不得成为终态契约。

### 3.4 不采用 D：宿主子进程托管

backend/Tauri 拉起并监督 Hermes 会产生启动顺序、退出码、崩溃重启、升级和数据目录归属耦合，违背“Hermes 独立后台服务”决策。真实 Hermes 由其自身 daemon/service 方式启动；backend 只做 discovery、调用、webhook 接收和事实恢复。

## 4. 终态进程拓扑与数据流

```text
Android App
   │ TRTC 音频
   ▼
sidecar（Electron） ── WS ──► rtc_bridge（:19092）
                                  │
                                  ▼
                         MiniCPM-o / ApmBridge
                                  │ on_text（文本；正式 brain 接线待实现）
                                  ▼
backend FastAPI（:8000）
├─ 表现层：routes_brain / routes_brain_events / routes_ws
├─ 业务层：BrainPipeline + TaskService + RecoveryService
├─ 后端适配层：AgentBackend
│  ├─ ClipboardInjector（codex_gui，默认兼容）
│  └─ HermesA2ABackend（A2A HTTP，M4）
├─ 数据层：TaskStore（JSON，原子替换）+ 审计/去重记录
└─ EventBus → WS Hub + VoiceReportQueue
                                  │ A2A HTTP/JSON
                                  ▼
Hermes 独立进程（daemon/A2A server，Windows 或 WSL2）
├─ Hermes 自有 tools/skills/memory/scheduler
└─ signed webhook ── HTTP POST ──► backend /api/v1/brain/events
```

核心流：

```text
on_text(text, session_id)
 → POST /api/v1/brain/intent（或内部 service 调用）
 → 本地 R2 意图提取 + R3 脱敏
 → BrainTask(intent_ready)
 → 风险/置信度门禁
    ├─ direct_reply：不创建可执行任务，回语音前台
    ├─ clarify：追问，不调用 AgentBackend
    ├─ deny：拒绝并审计，不调用 AgentBackend
    └─ delegate：low 自动派发；high 等待确认
 → AgentBackend.delegate(task, idempotency_key)
 → queued/running（事实先落盘）
 → Hermes signed webhook → 验签/去重/映射
 → completed/failed/needs_input/cancelled
 → EventBus → WS + 待播报队列 → 下一次语音会话取一次
```

## 5. 模块边界、目录和依赖门禁

### 5.1 目标目录（新增/迁移文件）

```text
backend/app/brain/
├─ schemas.py                 # BrainTask/请求/内部事件 schema；仅类型
├─ pipeline.py                # 状态机编排；不导入具体后端
├─ store.py                   # JSON 读写、迁移、原子保存、查询
├─ injector.py                # 兼容导出：Injector 别名/迁移过渡
├─ backends/
│  ├─ __init__.py             # AgentBackend、DelegateResult、CancelResult
│  ├─ clipboard.py            # ClipboardInjector，保留现有行为
│  ├─ _win32.py               # Win32 焦点/剪贴板/SendInput 底层（超 300 行时拆）
│  ├─ hermes_a2a.py           # Hermes A2A adapter；先探针契约，后真实实现
│  └─ protocol_types.py       # adapter 内部 placeholder，不冒充官方 schema
├─ events.py                  # Brain 领域事件组装（可选，避免 pipeline 继续膨胀）
├─ recovery.py                # 启动恢复、远端事实不确定处理
├─ webhook.py                 # 原文 HMAC、时间窗、事件去重、映射
└─ report_queue.py             # 语音结果待播报与一次性消费
backend/app/api/
├─ routes_brain.py            # 现有 brain 端点，薄路由
├─ routes_brain_events.py     # POST /api/v1/brain/events，薄路由
└─ routes_ws.py               # 订阅 EVT_BRAIN_TASK，并保持 WS 协议稳定
```

### 5.2 依赖方向

```text
routes_*（表现层）
  → BrainPipeline / RecoveryService / WebhookService（业务层）
    → AgentBackend / TaskStore / EventBus ports（接口/数据层）
      → Clipboard/HTTP client/JSON filesystem（基础设施）
```

硬门禁：入口 `main.py` 只装配；路由只校验参数、调用 service、组装统一响应；service 不 import FastAPI request/response；store 不做风险/状态业务判断；`pipeline.py` 不 import `ClipboardInjector` 或 `HermesA2ABackend`；每个源码文件 ≤300 行，超过按功能拆分而不是凑行数。

### 5.3 Injector → ClipboardInjector 迁移

1. 新建 `backends/clipboard.py::ClipboardInjector`，复制现有 `Injector` 的构造依赖和 `validate_focus/inject/write_fallback_file/audit` 行为。
2. 将 Win32 底层函数迁到 `_win32.py`，若迁移后 `clipboard.py` 仍超过 300 行继续拆分审计或文件兜底。
3. `backends/__init__.py` 定义 `AgentBackend` Protocol，并让 `ClipboardInjector` 实现 `delegate()`：内部顺序仍是焦点校验→剪贴板/SendInput→文件兜底，返回 `DelegateResult`。
4. `injector.py` 在一个迁移周期内保留 `Injector = ClipboardInjector` 兼容导出，旧 import 不立即破坏。
5. `main.py:150` 改为调用工厂选择 backend；缺少 `delegate.backend` 时默认 `codex_gui`，行为、审计和 confirm_token 规则保持不变。
6. `BrainPipeline` 构造参数从 `injector` 改为 `backend`；为现有测试提供同名 fake adapter，不在 pipeline 中兼容分支具体类。

## 6. AgentBackend 接口与内部契约

以下是本项目内部契约，不是 Hermes 官方 payload：

```python
class DelegateResult(TypedDict):
    ok: bool
    backend_id: Literal["codex_gui", "hermes_a2a"]
    channel: Literal["clipboard", "fallback_file", "a2a"]
    remote_task_id: str | None
    remote_context_id: str | None
    accepted_at: float | None
    error_class: str | None
    retryable: bool
    raw_reference: str | None  # 仅指向脱敏探针/审计，不放原始 secret

class AgentBackend(Protocol):
    backend_id: str
    async def discover(self) -> DiscoveryResult: ...
    async def delegate(self, task: BrainTask, *, idempotency_key: str) -> DelegateResult: ...
    async def cancel(self, task: BrainTask, *, idempotency_key: str) -> CancelResult: ...
    async def health(self) -> BackendHealth: ...
```

- `discover/cancel` 在 Clipboard 后端可返回明确 `unsupported`，不得伪造成功。
- `remote_task_id/context_id` 只填 adapter 已从真实响应确认的值；未确认时为 `None` 并进入 `recovering` 或 `failed`。
- `idempotency_key = "brain:" + task_id + ":v" + task.version`，同任务版本稳定；任务内容变化必须递增版本并使旧确认失效。
- Hermes adapter 内部可将 `BrainTask` 映射为 `AdapterTaskEnvelope`，但 envelope 字段和官方 A2A message 字段之间必须有显式转换层。

## 7. 语音文本路由与风险门禁

### 7.1 路由结果

内部 `RouteDecision` 精确字段：

```json
{
  "route": "direct_reply | delegate | clarify | deny",
  "confidence": 0.0,
  "sanitized_summary": "<=1200 字的脱敏摘要",
  "risk": "low | high | blocked",
  "missing": ["目标范围"],
  "clarifying_questions": ["请说明要检查哪个目录？"],
  "session_id": "string|null"
}
```

`confidence < configured threshold`、必需参数缺失或风险不明时强制 `clarify`/`high`；模型高置信度不能覆盖规则门禁。原始语音、完整原文、密钥、凭证、完整路径不进入 Hermes envelope 或普通日志。

### 7.2 语音接线边界

`PeerVoiceSession._on_text` 只负责将文本和会话标识交给 voice-to-brain service；不得把 AgentBackend、HTTP client 或状态迁移逻辑写进 `rtc_bridge/session.py`。该 service 负责：

1. 判断是否存在有效 `session_id`；缺失则生成本次语音会话关联 ID。
2. 调用 `BrainPipeline.create_intent` 或等价 application service。
3. 对 `direct_reply/clarify/accepted/awaiting_confirm` 生成短语音回报。
4. 后台终态不依赖原始连接，写入 `report_queue`，由下一次语音会话消费。

这条接线目前为待实现能力；不要把当前 `_on_text` 的日志事实表述为已完成闭环。

## 8. BrainTask 状态机与迁移表

### 8.1 任务字段（新增字段均有兼容默认值）

```json
{
  "task_id": "BT-20260822-001",
  "version": 1,
  "status": "intent_ready",
  "route": "delegate",
  "risk": "low",
  "intent": {"intent_type":"...", "target_app":"...", "confidence":0.9, "sanitized_summary":"..."},
  "subtasks": [],
  "instruction": null,
  "source": "voice",
  "session_id": "S-...",
  "delegate_backend": "codex_gui",
  "idempotency_key": "brain:BT-20260822-001:v1",
  "remote_task_id": null,
  "remote_context_id": null,
  "progress": null,
  "error": null,
  "error_class": null,
  "retryable": false,
  "created_at": 0.0,
  "updated_at": 0.0,
  "accepted_at": null,
  "completed_at": null,
  "confirm_token": null,
  "confirm_expires_at": null,
  "last_event_id": null,
  "seen_event_ids": [],
  "report_pending": false,
  "degraded": false
}
```

`seen_event_ids` 只保存有上限的去重窗口或迁移到独立 dedupe 文件；不得无限增长。`confirm_token` 仅内部校验，不进 UI 预览、语音文本或审计正文。

### 8.2 状态集合

`intent_ready | clarify | awaiting_confirm | queued | running | needs_input | completed | failed | cancelled | denied | expired | recovering`。

旧状态 `decomposed` 与 `injected` 必须可读：`decomposed` 映射为 `awaiting_confirm`；`injected` 迁移为 `queued`（若无远端事实则 `recovering`），保留 `legacy_status` 供审计。

### 8.3 合法迁移

| 当前 | 触发 | 下一状态 | 必备约束 |
|---|---|---|---|
| 新建 | 意图受理 | `intent_ready` | 唯一 task_id，摘要已脱敏 |
| `intent_ready` | 缺信息 | `clarify` | 至少一个问题，不可调用 backend |
| `intent_ready` | 高风险/需确认 | `awaiting_confirm` | version、风险、范围和过期时间已落盘 |
| `intent_ready` | 低风险自动派发 | `queued` | 先落盘幂等键，后调用 backend |
| `awaiting_confirm` | 明确确认 | `queued` | token/version/范围同时匹配 |
| `awaiting_confirm` | 拒绝 | `denied` | 审计确认拒绝，零 backend 调用 |
| `awaiting_confirm` | 超时 | `expired` | token 失效，零 backend 调用 |
| `queued` | backend 接受 | `running` | 只有 adapter 返回可关联事实才可进入 |
| `queued/running` | 需要输入 | `needs_input` | 保存短问题，不猜测执行结果 |
| `queued/running` | webhook/查询完成 | `completed` | 仅单向进入终态，保存 completed_at |
| `queued/running` | 可重试故障 | `recovering` | 退避有上限，不重复创建远端任务 |
| 任意非终态 | 不可重试故障 | `failed` | error_class/retryable=false |
| `queued/running` | 取消事实确认 | `cancelled` | 必须区分 requested 与 confirmed |
| `recovering` | 恢复事实未知 | `recovering/needs_input/failed` | 不伪报完成 |

终态 `completed/failed/cancelled/denied/expired` 不允许回退到 `running`，重复 webhook 只能返回幂等成功而不重复播报。任务内容变化必须新版本；旧确认令牌和旧 idempotency key 失效。

## 9. JSON 持久化与兼容策略

### 9.1 原子写入

`TaskStore` 保留现有 JSON 文件位置 `backend/data/brain_tasks.json`，写入采用：同目录临时文件→flush→`os.replace`；进程启动时若主文件损坏，优先读取 `.bak`，仍失败才从空开始并生成结构化告警。任何状态迁移先在内存副本校验，再一次性保存，避免半个任务对象落盘。

### 9.2 版本化迁移

- 顶层仍兼容历史“数组”格式；可在下一迁移版本引入 `{ "schema_version": 2, "tasks": [...] }`，读取器同时支持数组和对象。
- 缺少字段默认：`version=1`、`delegate_backend="codex_gui"`、远端/进度/错误/去重/播报字段为 `None`/空值。
- `decomposed`→`awaiting_confirm`、`injected`→`recovering`（启动恢复时依据是否存在 `remote_task_id` 决定 `queued` 或 `recovering`），不得删除历史任务。
- 未知字段读取时忽略、写回时保留 `extensions` 命名空间，防止新旧版本互相破坏。
- JSON 不是高并发数据库；MVP 单 backend 单写入者，使用进程内锁；多实例部署和高频 webhook 前必须迁移 PostgreSQL/Redis，不能默默扩展 JSON。

## 10. Hermes Agent Card discovery 与 A2A adapter

### 10.1 Discovery 门禁

backend 启动或配置变更时调用 `HermesA2ABackend.discover()`，但 adapter 只能根据现场探针确认的 URL、method、状态码和 JSON schema 解析。配置中的 `agent_card_url` 可以作为候选地址，不得当作官方固定地址。

Discovery 必须验证并缓存以下内部结果：

```json
{
  "reachable": true,
  "endpoint": "脱敏后的 URL",
  "protocol": "待探针确认",
  "capabilities": ["待探针确认"],
  "auth_mode": "待探针确认",
  "card_fingerprint": "sha256",
  "observed_at": 0.0,
  "error_class": null
}
```

**待现场探针**：Agent Card 的精确路径、是否 `/.well-known/agent.json`、响应字段名、A2A 版本字段、能力字段、token 头名称、任务 endpoint、取消 endpoint、webhook 签名头和事件字段。ADR-023 中的路径/能力说明只能作为探针假设，不能写入实现常量。

Discovery 失败分类：`not_started`、`network_timeout`、`auth_failed`、`protocol_incompatible`、`invalid_card`、`unknown`。失败期间不允许状态进入 `queued/running`。

### 10.2 A2A request/response 映射

内部 envelope：

```json
{
  "task_id": "BT-...",
  "version": 1,
  "idempotency_key": "brain:BT-...:v1",
  "session_id": "S-...",
  "summary": "脱敏摘要",
  "constraints": ["目标范围约束"],
  "acceptance": ["验收条件"],
  "risk": "low|high",
  "reply_webhook": "仅在探针确认后填充的回报地址引用"
}
```

A2A 官方请求/响应采用 `HermesRequestPlaceholder` / `HermesResponsePlaceholder` 类型隔离；在现场探针前不固定 `message.parts`、`task.id`、`contextId`、`status.state` 等字段。适配器要求：

1. 发送前只发送 envelope 中允许的脱敏字段。
2. HTTP 超时不得直接判定远端未创建；先进入 `recovering`，使用相同幂等键的探针/状态查询恢复事实。
3. 只有响应中被探针确认的稳定远端 ID 才填 `remote_task_id`。
4. 未知字段保留脱敏摘要和 hash，不记录完整 body。

## 11. 幂等、webhook HMAC、重放与事件去重

### 11.1 幂等

- backend 侧唯一键：`(task_id, version)`；同一版本仅允许一个 active remote association。
- A2A 请求携带 adapter 可确认的幂等位置；若 Hermes 不支持显式幂等字段，必须以相同请求关联值 + backend recovery 查询保证“不重复创建”，并在 OPEN 项中由项目总监裁决。
- 网络超时、backend 重启、WS 断线都不能生成新 task_id；`remote_task_id` 已存在时绝不重新 create。

### 11.2 HMAC 验签

Webhook handler 必须按顺序：

1. 读取原始 request body bytes。
2. 从受保护配置取得 secret；缺 secret 直接 fail-closed。
3. 按探针确认的签名规范计算 HMAC-SHA256，使用常量时间比较。
4. 校验时间戳窗口（建议默认 300 秒，最终由项目总监确认）和签名版本。
5. 解析 JSON，验证内部必需字段和 `remote_task_id` 关联。
6. 事件去重后再进行状态迁移和播报。

**待现场探针**：签名头名称、签名串拼接格式、是否包含 timestamp、算法前缀、Hermes webhook URL 配置方式。

### 11.3 重放与去重

内部规范事件形状：

```json
{
  "event_id": "string（若官方没有稳定 ID 则由原文 hash+remote id+timestamp 生成）",
  "event_type": "session_activity|tool_completion|turn_completion|unknown",
  "remote_task_id": "string",
  "occurred_at": 0.0,
  "payload": {},
  "raw_hash": "sha256"
}
```

官方 `event_id` 是否存在属于待探针；若不存在，不能声称具有官方事件 ID，只能使用受限 TTL 的本地去重键。重复事件返回统一成功响应但不二次迁移、不二次播报；未知远端任务 ID fail-closed 并记录安全审计。

## 12. WS 与语音事件模型

事件总线新增并统一事件 envelope：

```json
{
  "type": "event",
  "event": "brain_task",
  "data": {
    "task_id": "BT-...",
    "session_id": "S-...",
    "status": "accepted|awaiting_confirm|queued|running|needs_input|completed|failed|cancelled|denied|expired|recovering",
    "occurred_at": 0.0,
    "summary": "面向用户的短摘要",
    "progress": {"label":"读取测试结果","percent":null},
    "error_class": null,
    "report_pending": true
  }
}
```

- `routes_ws.py` 的 `WsHub` 订阅 `EVT_BRAIN_TASK`、`EVT_BRAIN_REPORT`；保留现有会话/告警/授权事件格式。
- 语音播报只使用 `summary` 与短错误说明，不播报 token、HMAC、完整路径、完整工具输出。
- `accepted`、`awaiting_confirm`、`queued/running`、终态分别可有播报策略；后台更新不得把 TRTC `connected/listening` 状态改写为任务状态。
- `report_queue` 持久化 `report_id/task_id/status/summary/created_at/consumed_at`；会话结束时 `report_pending=true`，下一次已鉴权会话按优先级取出并原子标记已消费。

## 13. 重启恢复、取消与人工接管

### 13.1 backend 重启

启动后扫描非终态任务：

- 无 `remote_task_id` 且 `queued`：若 dispatch_attempt 未确认，进入 `recovering`，使用同一幂等键重试一次；超过上限转 `failed`。
- 有 `remote_task_id`：优先使用探针确认的状态查询或等待 webhook；无法确认时保持 `recovering`。
- `awaiting_confirm`：按 `confirm_expires_at` 判断 `expired`，绝不自动执行。
- 终态任务只恢复播报队列，不重放副作用。

### 13.2 Hermes/backend/WS/App/sidecar 重启

Hermes 重启不触发新任务创建；backend 只通过 discovery/status/webhook 重建事实。WS、手机 App 或 sidecar 断线只影响传输，不影响任务状态。重连后先拉取持久化任务列表和未消费报告，再接收实时事件。

### 13.3 取消

`POST /api/v1/brain/tasks/{task_id}/cancel`（最终 OpenAPI 需写入）先校验归属、状态和 version，再调用 adapter `cancel()`。Hermes 取消方法/路径/请求字段/响应字段均待现场探针；若不支持，返回 `unsupported`，任务不伪装为 `cancelled`。已完成或已进入不可逆副作用阶段的任务只能进入 `needs_input`/人工接管说明。每次 cancel request、confirmed、unsupported、rejected 都写审计事件。

## 14. 错误分类与重试策略

| error_class | 示例 | retryable | 状态/动作 |
|---|---|---:|---|
| `config_missing` | endpoint/token/secret 缺失 | 否 | `failed`，启动门禁提示 |
| `not_started` | 连接拒绝/健康检查失败 | 是（有上限） | `recovering`→`failed` |
| `network_timeout` | connect/read 超时 | 是 | 固定上限指数退避 |
| `auth_failed` | 401/403 或签名失败 | 否 | `failed`，不盲目重试 |
| `protocol_incompatible` | Agent Card/schema 不匹配 | 否 | `failed`，阻断切流 |
| `rate_limited` | 429/限流 | 是 | `recovering`，尊重 Retry-After（待探针） |
| `remote_needs_input` | 远端要求补充信息 | 否 | `needs_input`，语音追问 |
| `remote_cancel_unsupported` | 无取消能力 | 否 | 保持运行并明确告知 |
| `remote_failed` | 远端报告失败 | 按事件 | `failed` |
| `webhook_invalid_signature` | HMAC 错误/过期 | 否 | HTTP 拒绝，任务不变 |
| `webhook_duplicate` | 已处理事件 | 否 | 幂等成功，不播报 |
| `unknown_remote_task` | 无法关联 | 否 | fail-closed，安全审计 |
| `persistence_failed` | JSON 原子写失败 | 否 | 高风险阻断；低风险标记 degraded |

所有 HTTP client 必须设置 connect/read/total timeout；重试仅对网络/限流类，且不改变 task_id/version/idempotency_key。

## 15. 配置与密钥边界

`BrainConfig` 新增非敏感配置（YAML 可存）：

```yaml
brain:
  delegate:
    backend: codex_gui
    discovery_timeout_s: 5
    request_timeout_s: 300
    max_retries: 2
    webhook_ttl_s: 300
    allowed_hosts: ["127.0.0.1"]
    hermes_a2a:
      base_url: ""
      agent_card_url: ""
```

环境变量/凭证存储（禁止写 YAML、任务、审计、日志）：

- `HERMES_A2A_TOKEN`
- `HERMES_WEBHOOK_SECRET`
- 如现场探针要求客户端证书，再增加证书路径/私钥引用，不把私钥内容写入代码。

网络默认只允许 loopback 或项目总监明确批准的 WSL2/内网地址；公网 endpoint、TLS、证书轮换和 webhook ingress 需单独安全裁决。日志只记录 host（必要时 hash）、状态码、错误分类、耗时和关联 ID，不记录 token、secret、原始 payload。

## 16. API 端点与统一响应

统一响应沿用现有约定：`{"code":0,"data":{},"message":""}`；错误响应必须包含业务错误码和人类可读 message，不包含内部堆栈/secret。

| Method | Path | 用途 | 当前/设计状态 |
|---|---|---|---|
| POST | `/api/v1/brain/intent` | 语音/文本路由和任务草稿 | 现有，扩展 route/risk |
| POST | `/api/v1/brain/task` | 拆解/生成确认材料 | 现有，保留兼容 |
| POST | `/api/v1/brain/inject` | 兼容确认入口；内部调用 AgentBackend | 现有，语义逐步改名 delegate |
| GET | `/api/v1/brain/tasks` | 分页任务列表 | 现有，扩展字段 |
| GET | `/api/v1/brain/tasks/{task_id}` | 任务详情/时间线 | 设计新增 |
| POST | `/api/v1/brain/tasks/{task_id}/confirm` | 高风险确认 | 设计新增或兼容 inject |
| POST | `/api/v1/brain/tasks/{task_id}/cancel` | 取消请求 | 设计新增 |
| POST | `/api/v1/brain/events` | Hermes signed webhook | 设计新增 |
| GET | `/api/v1/brain/backend/health` | discovery/后端健康 | 设计新增，禁止泄露凭证 |
| WS | `/ws/pet` | UI 任务事件 | 现有，新增订阅 |

请求 JSON Schema（内部设计，OpenAPI 最终稿必须与实现同步）：

```json
{
  "text": "string, 2..2000",
  "source": "voice|text",
  "target_app": "string|null",
  "session_id": "string|null",
  "client_request_id": "string|null"
}
```

高风险确认：`{task_id, version, decision: confirm|deny, confirm_token, session_id}`。取消：`{task_id, version, reason}`。Webhook body 不在本稿固定官方字段；只接受 adapter 解析后的内部事件形状。

建议错误码：`40001` 参数错误、`40101` 未授权、`40401` 任务不存在、`40901` 状态迁移冲突、`40902` 幂等键冲突、`42201` 确认令牌失效、`42901` 重生成/请求限频、`50201` Hermes 协议错误、`50202` Hermes 鉴权失败、`50203` Hermes 未启动/超时、`50301` brain 未初始化、`50302` 后端不可用、`50001` 持久化失败。

## 17. 测试分层与真实 HTTP 联调门禁

### 17.1 单元测试

- route/risk：四种 route、阈值、缺参、blocked 规则。
- 状态机：合法/非法迁移、终态不可回退、版本和确认 token 失效。
- `TaskStore`：旧数组、新字段默认、损坏恢复、`.bak`、原子替换模拟失败。
- 幂等：同 task/version 重试不重复 delegate；同 webhook event 不重复迁移/播报。
- HMAC：正确签名、错误签名、原文签名、过期 timestamp、缺 header、常量时间比较路径。
- `ClipboardInjector`：焦点校验、剪贴板竞态、文件兜底和旧行为回归。

### 17.2 集成/契约测试

- Fake `AgentBackend` 验证 pipeline 不 import 具体实现。
- Hermes adapter placeholder schema 测试：只验证内部 envelope 映射，不宣称官方字段。
- Webhook route→service→store→EventBus→WS hub 全链路。
- voice text service→brain 路由，验证 `session_id` 关联且原始语音不外传。

### 17.3 真实 HTTP 联调放行条件（P0）

必须同时具备：

1. 本机启动真实 Hermes 独立进程（或项目总监批准的 WSL2 进程），记录版本、启动命令、PID、监听地址。
2. 现场探针保存 Agent Card 原文 hash、请求 method/path、状态码和脱敏 schema；逐项标记已验证/不支持。
3. 使用真实 token 调 discovery；错误 token 必须得到可分类失败。
4. 使用真实 A2A 调用创建一个低风险只读任务，记录 `task_id`、幂等键、远端 ID、状态响应和 webhook 原文 hash。
5. 重发同一幂等请求，证明不产生第二个有效远端关联。
6. 发送错误 HMAC、过期签名和重复 webhook，证明 fail-closed、任务只更新一次、语音只播报一次。
7. backend/Hermes/WS/sidecar/App 至少各重启一次，证明恢复路径不重复执行、不伪报完成。
8. 取消探针：若官方不支持，必须记录“unsupported”并验证前台文案与状态准确。

未完成上述门禁，配置不得切换为 `hermes_a2a`，UI 不得显示“已接入 Hermes”。

## 18. 分阶段交付与回滚

### Phase 2A：抽象与兼容迁移

- 产出 `AgentBackend`、`DelegateResult`、`ClipboardInjector`、兼容导出和字段默认值。
- `BrainPipeline` 改为依赖抽象，默认 `codex_gui`。
- `TaskStore` 原子保存和旧 JSON 迁移。
- 回归：现有 `/api/v1/brain/*` 行为不变。

### Phase 2B：任务事实与回报基础设施

- 扩展状态机、事件 envelope、WS brain 订阅、report queue、recovery、审计和错误分类。
- 先用 FakeBackend 验证状态闭环。

### Phase 2C：Hermes 现场探针

- 不改业务路由；新增探针脚本/测试夹具，确认 Agent Card、A2A request/response、webhook HMAC、事件、取消、幂等能力。
- 任何探针发现与 ADR/本稿冲突，先更新本稿和 OPEN 项，再实现 adapter。

### Phase 2D：Hermes adapter 与灰度

- 实现 `HermesA2ABackend`，只解析探针确认字段。
- 先 discovery-only，再内部测试用户，最后低风险白名单灰度；高风险永远保留确认门禁。
- 监控 discovery 成功率、A2A 超时、重复事件、状态恢复、webhook 验签失败和播报去重。

### 回滚

1. 运行时将 `brain.delegate.backend` 切回 `codex_gui`，不删除任务事实和审计。
2. 新 Hermes 任务若远端事实未知，保持 `recovering`，禁止直接改成 `failed` 或重新注入 Codex。
3. 停止接收 Hermes webhook 前先保留安全拒绝响应；不得删除 dedupe 记录。
4. Hermes adapter 不加载不影响 ClipboardInjector；解除 Hermes endpoint/token 不影响旧路径。
5. 回滚后的 UI 明确显示“后台委派已暂停”，不把 Codex GUI 注入伪装成 Hermes 完成。

## 19. 技术约束清单（实现前 P0）

- 所有 API 路径带 `/api/v1/`，统一响应格式，最终输出 `openapi.yaml` 作为唯一契约。
- 不把未验证的 Hermes endpoint、payload、事件 ID、取消接口写成事实；所有 adapter placeholder 必须可追溯到探针证据。
- 不发送原始语音、密钥、凭证、完整敏感路径；日志与审计默认脱敏。
- HMAC 对原始 body 验签、常量时间比较、时间窗和去重先于状态更新。
- 高风险任务确认绑定 task/version/token/范围/过期时间；确认失败零 Hermes 调用。
- 状态事实与 TRTC、监听、语音会话状态分开；终态不可回退。
- 幂等键稳定，超时不盲目重建远端任务；未知事实进入 recovering。
- JSON 原子替换、旧文件兼容、损坏恢复；单写入者假设显式记录。
- `main.py` 只装配；依赖向下；模块单一职责；单文件 ≤300 行。
- 图标统一 Lucide SVG；禁止 emoji 作为功能图标；禁止紫粉渐变方案。

## 20. OPEN 项（需项目总监裁决或现场探针）

1. Hermes 实际版本、启动命令、Windows/WSL2 运行目录和网络地址。
2. Agent Card 精确 URL、A2A method/path、认证 header、请求/响应 schema 和幂等字段。
3. signed webhook 精确 URL 注册方式、签名头、签名串、时间戳格式、事件 ID 和远端任务 ID字段。
4. Hermes 是否提供稳定状态查询和取消接口；不支持取消时的产品文案。
5. 低风险自动派发白名单最终范围（只读代码/日志/状态/分析，还是更多可逆操作）。
6. 高风险确认的确认有效期默认值（本稿建议 300 秒）与语音确认语法白名单。
7. `session_id` 缺失时由谁生成、跨重连是否保持，以及多设备归属校验。
8. 下一次语音会话播报一条还是多条待播报结果，优先级和过期策略。
9. JSON 继续作为单实例 MVP 存储的期限；何时迁移 PostgreSQL/Redis。
10. webhook 是否允许本机 loopback 入站；若 Hermes 在 WSL2，是否批准内网绑定及 TLS。
11. 旧 `/inject` 是否保留为兼容别名，还是新增 `/confirm` 后逐步弃用。

## 21. 端到端完成定义

在项目总监确认 OPEN 项并完成现场探针后，按下列顺序验收：

1. 启动 backend、真实 Hermes、rtc_bridge/sidecar，核对每个 PID、健康接口和监听地址。
2. 通过语音或文字提交纯问答，断言无可执行 BrainTask、无 Hermes HTTP 请求。
3. 提交低风险只读任务，断言落盘 `intent_ready→queued→running`，收到签名回报后 `completed`，WS 和下一次语音各按策略得到一次结果。
4. 提交高风险删除/外发任务，断言未确认时 Hermes 调用计数为零；明确确认后只执行当前 version。
5. 使用错误 token、错误 HMAC、过期 webhook、重复 webhook 和未知 remote ID，断言对应错误分类、fail-closed 和无重复播报。
6. 在 HTTP 超时、backend/Hermes/WS/App/sidecar 重启后重跑恢复，断言不重复执行、不伪报完成。
7. 对可取消和不可取消任务分别验证 `cancelled`、`unsupported` 或人工接管结果。
8. 保存脱敏日志、探针原文 hash、状态时间线和测试命令，作为 Phase 2→3 放行证据。

---

## 附：证据索引

- `specs/hermes-delegation-loop/requirements.md:35-57`：范围与明确不做。
- `requirements.md:91-131`：路由、风险、确认、幂等和状态事实规则。
- `requirements.md:198-235`：持久化、webhook HMAC、去重验收。
- `requirements.md:248-307`：恢复、取消、审计和测试要求。
- `docs/decisions/ADR-023-hermes-integration-boundary.md:27-47`：ADR 记录的 Hermes 能力事实（非本机联调证据）。
- `ADR-023-hermes-integration-boundary.md:123-167`：独立进程拓扑和 A2A 数据流裁决。
- `ADR-023-hermes-integration-boundary.md:175-270`：AgentBackend、迁移、配置和依赖方向预留。
- `backend/app/brain/schemas.py:12-85`：现有状态与 BrainTask 字段。
- `backend/app/brain/pipeline.py:52-73,136-257`：当前 BrainPipeline 编排与 Injector 依赖。
- `backend/app/brain/store.py:21-78`：现有 JSON store 和非原子写入事实。
- `backend/app/brain/injector.py:50-123`：Codex GUI 注入行为与审计。
- `backend/app/main.py:145-154`：当前 brain 装配点。
- `backend/app/core/events.py:38-55`：事件类型和 EventBus。
- `backend/app/api/routes_brain.py:18-103`：现有 Brain API。
- `backend/app/api/routes_ws.py:24-86`：当前 WS Hub 订阅范围。
- `backend/app/voice/apm_bridge.py:66-85,251-295`：文本回调和接收循环。
- `backend/rtc_bridge/session.py:109-120,212-214`：语音会话绑定与当前仅日志 `_on_text`。
- `backend/app/config.py:132-157,255-299`：当前配置模型和加载入口。
- `docs/master-roadmap.md:120-140`：J1/J2/J3 与 M4 融合路线。
