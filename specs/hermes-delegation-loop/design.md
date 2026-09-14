# Hermes 委派闭环技术设计

- **状态**：Phase 2 设计稿，待用户确认后进入实施任务拆解
- **日期**：2026-08-23
- **权威需求**：`specs/hermes-delegation-loop/requirements.md`
- **架构裁决**：`docs/decisions/ADR-023-hermes-integration-boundary.md`
- **设计输入**：`design-architecture-draft.md`、`design-interaction-draft.md`、`design-qa-draft.md`
- **范围**：单任务、单 Hermes 后端、独立进程、A2A over HTTP、signed webhook、状态与结果回报

> 本文只定义本项目内部契约和实现边界。Hermes 实际 Agent Card、A2A endpoint、请求/响应字段、webhook 签名格式、事件字段和取消能力均未完成本机现场探针；凡未探针确认的内容均不得写成官方事实或实现常量。

## 1. 设计结论

采用以下终态：

```text
Android 语音前台
  -> MiniCPM-o 文本回调
  -> backend voice-to-brain service
  -> 路由/脱敏/风险门禁
  -> AgentBackend
       |- ClipboardInjector (codex_gui，默认兼容)
       `- HermesA2ABackend (独立 Hermes + A2A HTTP)
  -> BrainTask 持久化状态
  -> signed webhook / recovery
  -> EventBus
       |- pet-ui WebSocket
       `- voice report queue
```

硬约束：

1. `BrainPipeline` 只依赖 `AgentBackend`，不导入 Hermes 或 Codex GUI 实现。
2. 默认 `delegate_backend` 为 `codex_gui`；Hermes 未通过探针和真实联调前不可切换。
3. 低风险只读/分析任务自动派发；高风险动作执行前必须绑定当前版本并获得明确语音确认；无法判定按高风险处理。
4. 原始语音、密钥、凭证、完整敏感路径和完整工具输出不进入 Hermes 请求、普通日志或语音播报。
5. A2A 超时不能直接判定远端未创建；未知事实进入 `recovering`，不得盲目重建任务。
6. 任务事实与 TRTC 连接、监听服务、语音会话事实严格分离。
7. 设计、fake server 通过或静态检查均不能替代真实 Hermes 七项现场探针。

## 2. 当前事实与非目标

### 2.1 已确认的代码事实

- `backend/app/brain/pipeline.py:52-73,136-257` 当前依赖具体 `Injector`，终点为 `confirm_inject()`。
- `backend/app/brain/injector.py:50-123` 当前通过 Win32 焦点、剪贴板和 `SendInput` 注入 Codex GUI。
- `backend/app/brain/store.py:21-78` 当前为内存字典加 JSON 文件，写入尚非原子替换。
- `backend/app/api/routes_brain.py:18-103` 当前提供 intent、task、inject 和 tasks 端点。
- `backend/app/main.py:145-154` 是 Brain 组件装配点。
- `backend/app/core/events.py` 已有 `EVT_BRAIN_*`，但 `routes_ws.py` 当前未广播任务事件。
- `backend/rtc_bridge/session.py:212-214` 的 `_on_text()` 目前只记录日志，语音文本到 brain 的接线尚未实现。
- `docs/openapi.yaml` 当前未定义 brain 委派、webhook、取消等完整契约。
- Hermes 当前只在 ADR 和监控配置中出现规划/监控边界，仓库没有已验证的 A2A client、Agent Card、webhook 或启动脚本。

### 2.2 本期不做

不 fork Hermes，不使用 ACP 作为 agent-to-agent 协议，不由 Tauri/sidecar/backend 托管 Hermes，不替换 MiniCPM-o 或 TRTC 媒体链，不通过 HTTP/WS 中继裸 PCM，不自研通用工具、技能、记忆、子 Agent 或调度引擎，不实现复杂多 Agent 规划和跨会话长期画像。

## 3. 方案裁决

| 方案 | 形态 | 结论 |
|---|---|---|
| A | Hermes 独立进程 + A2A HTTP + signed webhook | 采用。隔离故障域，兼容 Windows/WSL2，符合 ADR-023 |
| B | ACP stdio/JSON-RPC | 不采用。ACP 是 editor-to-agent 语义，不能推导 agent-to-agent 委派 |
| C | backend 进程内共享内存/内部 IPC | 不采用。升级、权限、生命周期和故障域耦合，不能自然跨 WSL2 |
| D | backend/Tauri 拉起 Hermes 子进程 | 不采用。违反独立服务边界，增加启动、退出和升级耦合 |

## 4. 模块与依赖边界

目标目录：

```text
backend/app/brain/
  schemas.py              # 类型、请求、内部事件、状态枚举
  pipeline.py             # 应用编排和状态迁移，不导入具体 backend
  store.py                # JSON 原子持久化、迁移、查询
  injector.py             # Injector 兼容导出
  backends/__init__.py    # AgentBackend、结果和健康类型
  backends/clipboard.py   # 现有 Codex GUI 行为迁移
  backends/_win32.py      # Win32 底层
  backends/hermes_a2a.py  # 现场探针后实现的 Hermes adapter
  backends/protocol_types.py # 未验证官方协议的隔离类型
  recovery.py             # 启动恢复和事实不确定处理
  webhook.py              # 原文 HMAC、时间窗、去重和事件映射
  report_queue.py         # 终态/行动项待播报队列
  events.py               # 统一 brain_task/report 事件组装
backend/app/api/
  routes_brain.py         # brain HTTP 薄路由
  routes_brain_events.py  # signed webhook 薄路由
  routes_ws.py            # 任务事件广播和快照/重放
```

依赖方向：

```text
routes -> application services -> ports/schemas/store/event bus -> filesystem/HTTP/Win32
```

`main.py` 只负责装配；路由不实现状态迁移；store 不实现风险判断；service 不依赖 FastAPI request/response；每个源码文件不超过 300 行，超出按职责拆分。

## 5. AgentBackend 内部契约

以下是本项目内部类型，不是 Hermes 官方 payload：

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
    raw_reference: str | None

class CancelResult(TypedDict):
    ok: bool
    outcome: Literal["confirmed", "requested", "unsupported", "rejected", "unknown"]
    error_class: str | None
    retryable: bool
    raw_reference: str | None

class AgentBackend(Protocol):
    backend_id: str
    async def discover(self) -> DiscoveryResult: ...
    async def health(self) -> BackendHealth: ...
    async def delegate(self, task: BrainTask, *, idempotency_key: str) -> DelegateResult: ...
    async def cancel(self, task: BrainTask, *, idempotency_key: str) -> CancelResult: ...
    async def recover(self, task: BrainTask) -> RecoveryResult: ...
```

`ClipboardInjector` 保留现有焦点校验、剪贴板、SendInput、文件兜底和审计行为，并通过 `Injector = ClipboardInjector` 保持旧 import。`discover/cancel/recover` 对该后端返回明确 `unsupported`，不得伪造远端成功。

幂等键固定为：

```text
brain:<task_id>:v<version>
```

同一 `(task_id, version)` 只能有一个 active remote association。任务内容变化必须生成新版本，使旧确认令牌和旧幂等键失效。

## 6. 语音路由与风险门禁

`voice-to-brain service` 是语音与 Brain 的唯一接线层。`rtc_bridge/session.py` 只传递文本和 `session_id`，不承载 HTTP client 或状态机。

内部 `RouteDecision`：

```json
{
  "route": "direct_reply|delegate|clarify|deny",
  "confidence": 0.0,
  "sanitized_summary": "不超过 1200 字的脱敏摘要",
  "risk": "low|high|blocked",
  "missing": [],
  "clarifying_questions": [],
  "session_id": "string|null"
}
```

规则顺序：

1. 先做输入校验、意图提取和脱敏。
2. `direct_reply` 不创建可执行任务、不调用 backend。
3. 缺目标/范围/权限/验收条件进入 `clarify`，至少提供一个可回答问题。
4. 安全边界不可接受进入 `deny`，记录原因且零 backend 调用。
5. `delegate + low` 先持久化草稿和幂等键，再自动派发。
6. `delegate + high` 先进入 `awaiting_confirm`，语音说明动作、目标、风险和有效期；确认前调用计数必须为零。
7. 风险不明、置信度不足或模型与规则冲突时按 `high`/`clarify` 处理，模型高置信度不能绕过规则。

## 7. BrainTask 状态、字段与迁移

### 7.1 持久化字段

```json
{
  "task_id": "BT-20260823-001",
  "schema_version": 2,
  "version": 1,
  "status": "intent_ready",
  "route": "delegate",
  "risk": "low",
  "intent": {},
  "subtasks": [],
  "instruction": null,
  "source": "voice",
  "session_id": "S-...",
  "delegate_backend": "codex_gui",
  "idempotency_key": "brain:BT-20260823-001:v1",
  "remote_task_id": null,
  "remote_context_id": null,
  "dispatch_attempt": 0,
  "retry_count": 0,
  "next_retry_at": null,
  "last_error_class": null,
  "progress": null,
  "cancel": {
    "requested_at": null,
    "requested_by": null,
    "outcome": null,
    "confirmed_at": null,
    "reason": null
  },
  "confirm_token": null,
  "confirm_expires_at": null,
  "last_event_id": null,
  "seen_event_ids": [],
  "report_pending": false,
  "created_at": 0.0,
  "updated_at": 0.0,
  "accepted_at": null,
  "completed_at": null,
  "degraded": false,
  "extensions": {}
}
```

`seen_event_ids` 受上限约束；超过窗口迁移到独立 dedupe 记录。`confirm_token` 只用于内部校验，不进入 UI、语音、审计正文或错误响应。`dispatch_attempt`、`retry_count`、`next_retry_at` 必须同时存在于 schema、JSON、事件和 API response，不能只写在恢复文档中。

### 7.2 状态集合

```text
intent_ready, clarify, awaiting_confirm, queued, running, needs_input,
completed, failed, cancelled, denied, expired, recovering
```

旧 `decomposed` 可读并映射为 `awaiting_confirm`；旧 `injected` 在启动恢复时：存在远端 ID 则进入 `recovering` 并恢复，否则按旧行为记录为 `queued`/兼容历史状态。保留 `legacy_status`，不得产生第二套事实。

### 7.3 合法迁移

| 当前 | 触发 | 下一状态 | 约束 |
|---|---|---|---|
| 新建 | 受理 | `intent_ready` | 唯一 task_id，摘要脱敏 |
| `intent_ready` | 缺信息 | `clarify` | 有追问，零 backend 调用 |
| `intent_ready` | 高风险 | `awaiting_confirm` | token、version、范围和过期时间先落盘 |
| `intent_ready` | 低风险 | `queued` | 草稿和幂等键先落盘 |
| `awaiting_confirm` | 明确确认 | `queued` | token/version/范围全部匹配 |
| `awaiting_confirm` | 拒绝 | `denied` | 审计，零 backend 调用 |
| `awaiting_confirm` | 超时/版本变化 | `expired` | 旧 token 失效，零 backend 调用 |
| `queued` | backend 已接受 | `running` | 仅有可关联事实时允许 |
| `queued/running` | 需要补充信息 | `needs_input` | 保存短问题，不猜结果 |
| `queued/running` | 完成事实 | `completed` | 保存结果和 completed_at |
| `queued/running` | 可重试错误 | `recovering` | 有上限退避，不创建副本 |
| 任一非终态 | 不可重试错误 | `failed` | 保存 error class |
| `queued/running` | 取消确认 | `cancelled` | cancel outcome 为 confirmed |
| `recovering` | 事实恢复 | `queued/running/completed/failed/needs_input` | 依现场协议决定 |

`completed/failed/cancelled/denied/expired` 不得回退。重复或迟到 webhook 只能幂等返回，不重复状态、副作用或播报。

## 8. JSON 持久化与恢复

`TaskStore` 保留 `backend/data/brain_tasks.json`，采用同目录临时文件、flush、`os.replace`，并维护 `.bak`。读取器同时支持旧数组和 `{schema_version, tasks}` 对象；缺字段使用兼容默认值，未知字段保留在 `extensions`。

单实例 MVP 使用进程内写锁。高频 webhook 或多实例部署前必须迁移 PostgreSQL/Redis，不得默默扩大 JSON 的并发承诺。

启动恢复规则：

- `awaiting_confirm` 按 `confirm_expires_at` 过期，绝不自动执行。
- `queued` 且无远端 ID：增加 `dispatch_attempt`，进入 `recovering`，使用同一幂等键最多重试配置上限。
- 有远端 ID：调用现场探针确认的 status/recover，或保持 `recovering` 等待 signed webhook。
- 超时、backend/Hermes/WS/App/sidecar 重启不生成新 task_id，不清除 remote_task_id。
- 终态只恢复待播报队列，不重放副作用。
- 无法确认远端事实时保持 `recovering` 或 `needs_input`，不伪报完成、不自动回退到 Codex 注入。

## 9. Hermes discovery 与 A2A adapter

### 9.1 探针前允许的内容

配置只允许候选 `base_url`、`agent_card_url` 和受保护 token。adapter 使用 `protocol_types.py` 隔离 placeholder；不能在代码中固定 `/.well-known/agent.json`、具体 method/path、`message.parts`、`task.id`、`contextId`、状态字段、取消字段或签名头，除非现场探针产生证据。

Discovery 内部结果：

```json
{
  "reachable": false,
  "endpoint": "脱敏 URL",
  "protocol": "unverified",
  "capabilities": [],
  "auth_mode": "unverified",
  "card_fingerprint": null,
  "observed_at": 0.0,
  "error_class": "not_started|network_timeout|auth_failed|protocol_incompatible|invalid_card|unknown"
}
```

发现失败时禁止进入 `queued/running`，也不能向用户显示“已接入 Hermes”。

### 9.2 内部 envelope

```json
{
  "task_id": "BT-...",
  "version": 1,
  "idempotency_key": "brain:BT-...:v1",
  "session_id": "S-...",
  "summary": "脱敏摘要",
  "constraints": [],
  "acceptance": [],
  "risk": "low|high",
  "reply_webhook_ref": "仅探针确认后填充"
}
```

Hermes 官方请求/响应只能由 adapter 转换。HTTP connect/read/total timeout 必须独立配置。响应无稳定远端 ID时不可写入 `remote_task_id`；超时先进入 `recovering`。

## 10. Webhook 安全与事件去重

handler 必须按以下顺序处理：

1. 读取原始 body bytes。
2. 从受保护配置读取 secret，缺少则 fail-closed。
3. 按现场探针确认的签名头、拼接格式和算法，用常量时间比较 HMAC-SHA256。
4. 校验 timestamp 时间窗，默认建议 300 秒，最终以探针和产品裁决为准。
5. 解析并校验内部必需字段，关联 `remote_task_id`。
6. 持久化去重记录后再迁移状态、写审计和入队播报。

内部标准事件：

```json
{
  "event_id": "官方稳定 ID 或受限 TTL 本地派生键",
  "event_type": "session_activity|tool_completion|turn_completion|unknown",
  "remote_task_id": "string",
  "occurred_at": 0.0,
  "payload": {},
  "raw_hash": "sha256"
}
```

签名头名称、签名串、事件 ID 是否官方字段、事件 payload、webhook 注册方式均为待探针。错误签名、过期签名、body 重排、未知 remote ID 和重放事件均 fail-closed；重复事件可返回幂等成功但不得有第二次状态迁移或播报。

## 11. 取消契约

端点：`POST /api/v1/brain/tasks/{task_id}/cancel`。

请求：

```json
{
  "version": 1,
  "reason": "用户请求取消"
}
```

响应必须包含：`task_id`、`version`、`status`、`cancel.outcome`、`cancel.requested_at`、`cancel.confirmed_at`、`retryable` 和安全错误码。

处理规则：

- 先校验用户归属、version 和当前状态，再写 `cancel.requested_at` 审计。
- adapter 返回 `confirmed` 后才迁移 `cancelled`。
- 返回 `requested` 时任务保持 `queued/running`，前台显示“取消请求已提交”。
- 返回 `unsupported` 时不改为 `cancelled`，显示“无法撤销，只能等待结果/人工处理”。
- 已完成、已取消、已拒绝、已过期任务取消请求幂等返回，不产生副作用。
- 不可逆执行阶段不得伪装取消，必须给出人工接管或等待结果说明。
- Hermes 取消 method/path/payload/response 未探针确认前只实现 adapter placeholder 和 fake contract。

## 12. 事件、WS 与语音回报

统一内部事件：

```json
{
  "type": "event",
  "event": "brain_task|brain_report",
  "data": {
    "event_id": "string",
    "task_id": "BT-...",
    "session_id": "S-...",
    "version": 1,
    "status": "queued|running|needs_input|completed|failed|cancelled|denied|expired|recovering",
    "occurred_at": 0.0,
    "summary": "脱敏短摘要",
    "progress": {"current": 0, "total": null, "label": null},
    "risk": "low|high|blocked",
    "requires_action": "confirm|provide_input|retry|cancel|none",
    "error_class": null,
    "result_summary": null,
    "report_pending": false
  }
}
```

任务事件不得包含 TRTC/监听服务字段；客户端保持 `task.status`、`trtc.connection`、`listener.service`、`voice.experience` 四个命名空间。`WsHub` 新增任务订阅前，客户端不得使用 `session_updated` 或连接徽标猜测任务状态。

### 12.1 report_queue 契约

记录按任务维度聚合，而非每个中间事件一条：

```json
{
  "report_id": "RP-...",
  "task_id": "BT-...",
  "version": 1,
  "status": "completed|failed|cancelled|denied|expired|needs_input|awaiting_confirm",
  "summary": "脱敏短摘要",
  "priority": "action|error|completion|cancel",
  "created_at": 0.0,
  "read_at": null,
  "announced_session_id": null,
  "ack_at": null
}
```

入队在终态或需要用户行动时发生；同一 `task_id + version + status` 幂等。取出时按优先级原子 claim，TTS 成功开始播放后写 `announced_session_id`，播放失败不标已播报；客户端查看详情或明确标记后写 `read_at`。WS 收到不等于已读。下一次语音最多播报两条行动/最新终态摘要，其余提示数量并引导任务列表。用户停止播报不取消任务。

## 13. API/OpenAPI 契约

最终必须更新 `docs/openapi.yaml`，每个端点独立描述 request、response、认证、状态码和统一错误模型；不能只保留泛化请求 JSON。

统一成功响应：

```json
{"code": 0, "data": {}, "message": ""}
```

统一错误响应：

```json
{"code": 40901, "message": "状态迁移冲突", "request_id": "..."}
```

必须定义以下端点：

| Method | Path | Request 必填 | Response 必填 |
|---|---|---|---|
| POST | `/api/v1/brain/intent` | `text, source, target_app?, session_id?, client_request_id?` | `task_id, route, risk, confidence, sanitized_summary, status` |
| POST | `/api/v1/brain/task` | `task_id, regenerate?` | `task`、确认材料或澄清信息 |
| POST | `/api/v1/brain/inject` | 兼容 `task_id, decision, confirm_token, version?` | `task_id, status, backend_id, result/error` |
| GET | `/api/v1/brain/tasks` | `status?, page?, limit?` | `items, page, limit, total` |
| GET | `/api/v1/brain/tasks/{task_id}` | 路径 task_id | 完整脱敏任务、时间线、report 状态 |
| POST | `/api/v1/brain/tasks/{task_id}/confirm` | `version, decision, confirm_token, session_id?` | 当前 task、status、audit reference |
| POST | `/api/v1/brain/tasks/{task_id}/cancel` | `version, reason?` | 当前 task、cancel outcome、retryable |
| POST | `/api/v1/brain/events` | 原始 webhook body + 探针确认签名头 | `accepted, duplicate`，不得泄露内部详情 |
| GET | `/api/v1/brain/backend/health` | 无 | discovery/health 脱敏结果 |
| WS | `/ws/pet` | 现有认证 | `brain_task`、`brain_report`、快照/重放事件 |

错误码至少包括：`40001` 参数错误、`40101` 未授权、`40401` 任务不存在、`40901` 状态冲突、`40902` 幂等冲突、`42201` token 失效、`50201` 协议错误、`50202` 鉴权失败、`50203` 未启动/超时、`50301` brain 未初始化、`50302` backend 不可用、`50001` 持久化失败。

## 14. 配置与密钥

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

`HERMES_A2A_TOKEN`、`HERMES_WEBHOOK_SECRET` 和可能的客户端证书只来自环境/凭证存储，不写 YAML、任务、审计和日志。默认只允许 loopback 或明确批准的 WSL2/内网地址。公网、TLS、证书轮换和 webhook ingress 需要单独安全裁决。

## 15. 测试与真实联调门禁

### 15.1 测试层级

- Unit：路由、风险、脱敏、状态迁移、schema、HMAC、错误分类。
- Property：终态不可回退、重复/乱序事件、确认 token/version、幂等。
- Integration：FakeBackend、真实临时 TaskStore、真实 EventBus、真实 WS client、loopback HTTP fake server。
- Contract：Agent Card、内部 envelope、adapter response、webhook schema；未探针部分只能验证 placeholder。
- E2E/recovery：真实 backend/HTTP socket/WS、App/sidecar/Hermes 进程重启，留 PID、health、transcript、store 快照和 TTS 计数。

### 15.2 P0 放行条件

1. 低风险任务先落盘后只派发一次；高风险确认前 backend 调用数为零。
2. 错误 HMAC、过期签名、body 重排、未知 remote ID、重复事件均 fail-closed 或幂等无副作用。
3. `remote_task_id`、task/version/idempotency_key 关联稳定；重启不创建副本。
4. JSON 无半文件；终态不回退；report_queue 不重复播报。
5. 原始语音、token、secret、凭证和完整敏感路径不出站、不入普通日志。
6. 真实 HTTP fake server 不能被 EventBus mock 替代；fake 通过不能写成 Hermes 已联通。
7. 真实 Hermes 七项探针全部留证：进程、健康、Agent Card、A2A 请求、webhook、任务状态、重复事件；另覆盖 Hermes kill/restart。
8. `docs/openapi.yaml` 与实现逐端点一致；恢复字段、取消 outcome 和 report_queue 字段全部进入 schema、持久化、事件和 API。
9. 不得删减测试断言、增加 skip/xfail/.only 或放宽覆盖率/质量门禁。

未完成真实探针前，配置保持 `codex_gui`，UI 不得显示“已接入 Hermes”。

## 16. 分阶段实施与回滚

### Phase 2A：抽象兼容

实现 `AgentBackend`、`DelegateResult`、`ClipboardInjector` 兼容迁移、TaskStore 原子写和历史 JSON 迁移；默认 codex 行为必须回归通过。

### Phase 2B：事实与回报

实现状态机、恢复/重试字段、统一事件、WS 广播、report_queue、审计和 FakeBackend 集成闭环；此阶段不声称 Hermes 已接入。

### Phase 2C：现场探针

新增探针脚本/fixture，记录真实 Hermes 版本、PID、启动方式、health、Agent Card、A2A、webhook、取消和幂等结果。探针与 ADR 冲突时先更新设计和 OPEN 项。

### Phase 2D：adapter 灰度

只解析探针确认字段；先 discovery-only，再内部低风险只读灰度。高风险确认门禁永久保留。监控鉴权失败、超时、重复事件、恢复、验签失败和播报去重。

### 回滚

将 `brain.delegate.backend` 切回 `codex_gui`；未知远端事实保持 `recovering`，不重注入 Codex、不删除审计和去重记录；UI 显示“后台委派已暂停”，不把 Codex 结果伪装成 Hermes 完成。

## 17. 待项目总监/现场探针裁决的 OPEN 项

1. Hermes 实际版本、启动命令、工作目录、进程名、Windows/WSL2 地址。
2. Agent Card URL、A2A method/path、鉴权 header、请求/响应 schema、幂等位置。
3. webhook URL 注册、签名头、签名串、timestamp 格式、事件 ID、remote_task_id 字段。
4. status/recover/取消能力及不可取消产品文案。
5. 低风险自动派发白名单是否严格限于只读代码/日志/状态/分析。
6. 置信度阈值、确认有效期和语音肯定句白名单。
7. `session_id` 生成、跨重连保持和多设备归属校验。
8. report_queue 多条播报、已读/已播报的最终产品策略。
9. JSON MVP 的保留期限和迁移 PostgreSQL/Redis 的触发条件。
10. WSL2 webhook 入站、内网绑定、TLS 和证书轮换策略。
11. 旧 `/inject` 兼容保留周期。
12. 任务详情在 Android 与桌宠的最终展示粒度及图标库锁定。

## 18. 设计完成定义

设计阶段完成必须满足：

- requirements、架构、交互、QA 四源一致；
- API、状态、恢复重试、取消和 report_queue 字段已统一；
- Hermes 未验证协议全部隔离并标记探针边界；
- 默认 codex 回退和回滚路径明确；
- 测试矩阵和真实进程/HTTP/WS/回报证据门禁明确；
- 用户确认本 `design.md` 后才编写 `tasks.md`，确认前不修改业务源码。
