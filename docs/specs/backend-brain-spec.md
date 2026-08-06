# 后端规格 — 大脑闭环（V1.5）：DeepSeek 客户端 / 混合路由 / 意图拆解 / 确认注入 / 语义跑偏 / 单向报告

> 版本：v1.0（V1.5 大脑闭环设计定稿）
> 日期：2026-08-03
> 状态：已确认 · 供后端 V1.5 照做（DeepSeek 实测参数见 §9，PoC 通过后回填）
> 依据：docs/decisions/OPEN-DECISIONS.md（O-011 混合大脑 / O-012 确认后注入 / O-013 受控注入）、docs/PRD.md v1.5（§5.2 / §5.4 / §6.6-6.8 / §7.3 / §9 / §10）、docs/specs/backend-llama-client-spec.md（本地引擎 SSE 契约）、docs/specs/backend-capture-auth-spec.md（WGC 授权）、docs/specs/push-provider-spec.md（PushManager 契约）、backend/app/core/orchestrator.py、backend/app/config.py、backend/app/core/state.py
> 关联决策：DeepSeek V4 Flash 官方发布（2026-07-31，base_url `https://api.deepseek.com`，model `deepseek-v4-flash`，OpenAI 兼容 `/chat/completions`）；价格 input $0.14/1M / output $0.28/1M（cache hit $0.0028/1M）——满足"省钱到极致"

---

## 1. 目标与背景

V1.5 把产品从"监控闭环"升级为"大脑闭环"：用户自然语言意图 → 本地 9B 理解并生成脱敏摘要 → DeepSeek 拆解为可执行任务 + 生成对 Codex 最优指令 → 用户确认 → 受控注入 Codex。同时把跑偏判断从"3 帧不变"升级为"语义级评审"，并把任务进度单向报告到手机。

**成功长什么样**：用户在桌宠输入"帮我重构这个项目的数据层"，贾克斯产出 3-8 步可执行任务清单 + 一段可直接粘贴/注入 Codex 的指令文本，用户确认后自动进入 Codex 输入框并发送；全程截图不出本机、仅脱敏摘要上云；任务执行中 DeepSeek 定期评审是否跑偏并给出修正建议。

**三条硬边界（O-013 受控注入）**：① 注入前用户显式确认；② 注入内容仅指令文本（不读 Codex 内部数据、不键鼠模拟 UI 之外操控）；③ 截图不出本机、上传云端仅脱敏会话摘要。

---

## 2. 分层与依赖（遵循 code-organization 硬规则）

```
backend/app/brain/                    # 🆕 新增包（大脑闭环业务层）
├── deepseek_client.py                # DeepSeek V4 Flash 客户端（OpenAI 兼容，≤300 行）
├── router.py                         # 混合大脑路由决策表（≤150 行）
├── sanitizer.py                      # 脱敏工具：路径/代码/key/长 token 替换（≤200 行）
├── schemas.py                        # Pydantic 模型：Intent/Subtask/Instruction/Review/BrainTask（≤200 行）
├── intent_service.py                 # 意图理解 + 摘要生成（本地 9B，≤250 行）
├── task_service.py                   # 拆解 + 指令生成（DeepSeek）+ 任务状态机（≤300 行）
├── injector.py                       # 确认后注入：剪贴板+SendInput / 备用文件通道（≤300 行）
├── offtrack_reviewer.py              # 语义级跑偏评审（DeepSeek，≤200 行）
├── reporting.py                      # 单向报告：任务进度/拆解结果/跑偏警告 → PushManager（≤150 行）
└── store.py                          # 任务仓库：内存 + JSON 持久化（≤200 行）

backend/app/api/routes_brain.py       # 🆕 新增路由（≤200 行，只编排不写业务）
backend/app/config.py                 # 改造：Settings 增 DEEPSEEK_* 字段 + BrainConfig
backend/app/core/events.py            # 改造：新增 EVT_BRAIN_* 事件常量
backend/app/main.py                   # 改造：lifespan 装配 brain 服务 + include_router（入口只装配）

backend/data/brain_tasks.json         # 🆕 任务持久化（运行时生成，不入库、不进 git）
backend/data/inject_audit.jsonl       # 🆕 注入审计日志（N-3 留痕，不含指令全文）
backend/data/instructions/            # 🆕 注入备用文件通道输出目录

config/brain.yaml                     # 🆕 大脑配置（deepseek/intent/review/inject/report 组）
config/prompts/intent_extract.md      # 🆕 本地 9B 意图提取 prompt（对齐 vision_analyze.md 风格）
config/prompts/decompose.md           # 🆕 DeepSeek 任务拆解 prompt（含 JSON schema）
config/prompts/instruct_codex.md      # 🆕 DeepSeek 指令生成 prompt（对 Codex 优化）
config/prompts/review.md              # 🆕 DeepSeek 语义跑偏评审 prompt
```

依赖方向：`routes_brain → task_service/intent_service → deepseek_client/store → schemas/sanitizer`；`reporting → push.manager`；`offtrack_reviewer → deepseek_client + store`。下层禁止反向 import 上层。

**单文件 ≤300 行硬上限**；`routes_brain.py` 不写业务逻辑（仅参数校验 → 调 service → 组装响应）；入口 `main.py` 只装配。

---

## 3. DeepSeek V4 Flash 客户端设计（`backend/app/brain/deepseek_client.py`）

### 3.1 配置与凭据（key 不入库）

- `DEEPSEEK_API_KEY` 仅存 `.env`，由 `Settings` 读取；**禁止**写入数据库、JSON 持久化、日志、metrics 属性。
- `config/brain.yaml` 提供非敏感配置；`Settings` 新增字段（从 `.env` 覆盖）：`DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`）、`DEEPSEEK_MODEL`（默认 `deepseek-v4-flash`）。

### 3.2 接口签名（OpenAI 兼容，httpx 非流式）

```python
# backend/app/brain/deepseek_client.py
class DeepSeekError(Exception): ...                                  # 聚合基类
class DeepSeekNetworkError(DeepSeekError): ...                       # httpx.TransportError → 可重试
class DeepSeekTimeoutError(DeepSeekNetworkError): ...                # 超时 → 可重试
class DeepSeekHttpError(DeepSeekError): ...                          # 4xx/5xx 非 JSON
class DeepSeekAuthError(DeepSeekHttpError): ...                      # 401/403 → 不可重试，报配置错误
class DeepSeekRateLimitError(DeepSeekHttpError): ...                 # 429 → 退避重试
class DeepSeekProtocolError(DeepSeekError): ...                      # 响应非 JSON / usage 缺失 → 不可重试

class DeepSeekClient:
    def __init__(self, settings: Settings, brain_cfg: BrainConfig) -> None: ...
    async def chat(self, messages: list[dict], *, max_tokens: int,
                   temperature: float = 0.2) -> str: ...
    async def chat_json(self, messages: list[dict], *, max_tokens: int,
                        json_schema: dict, temperature: float = 0.2) -> dict: ...
    async def health(self) -> bool: ...     # 轻量探测（空请求/超短请求），供 /health 与路由熔断判断
    def circuit_open(self) -> bool: ...     # 熔断状态查询
```

实现要点：
- 请求体：`POST {base_url}/chat/completions`，`{"model", "messages", "max_tokens", "temperature", "stream": false}`；`chat_json` 走 `response_format={"type":"json_object"}`（V4 Flash 支持 Json Output，官方文档确认）。
- `httpx.AsyncClient(timeout=...)` 显式传入，**不**隐式依赖环境代理（项目代理 127.0.0.1:7890 曾致故障，见 §11 已知坑）。

### 3.3 超时 / 重试 / 熔断

| 项 | 值 | 说明 |
|---|---|---|
| connect 超时 | 10s | 网络不通快速失败 |
| read / 总超时 | 30s / 60s | 拆解等长任务上限 |
| 网络类错误重试 | 2 次（退避 1s、2s） | `DeepSeekNetworkError`/`DeepSeekTimeoutError` |
| 429 / 5xx 重试 | 1 次（退避 3s） | `DeepSeekRateLimitError`/`DeepSeekHttpError(status>=500)` |
| 401/403 | 不重试 | 抛 `DeepSeekAuthError`，记 error 日志并提示"检查 DEEPSEEK_API_KEY" |
| 熔断 | 连续失败 ≥3 → 熔断 300s | 对齐 PushManager 熔断模式；熔断期间 `route()` 返回本地降级 |

### 3.4 Token 预算控制（省钱到极致）

| 调用类型 | max_tokens 上限 | 输入裁剪策略 |
|---|---|---|
| 拆解 `decompose` | 2048 | 仅携带脱敏摘要（≤1200 字）+ 意图（≤300 字）；超长截断（头 70% + 尾 30%，中间省略） |
| 指令生成 `instruct` | 2048 | 子任务清单 JSON（≤8 步）；逐项序列化，单步描述 ≤200 字 |
| 语义评审 `review` | 1024 | 当前子任务目标（≤200 字）+ 最近摘要（≤600 字） |
| 健康探测 `health` | 8 | 空 system 消息 |

- 输入侧：所有上传 DeepSeek 的文本**先过 `sanitizer` 再进 prompt**（见 §4.3），从源头控制 token 与隐私。
- 输出侧：`max_tokens` 上限即服务端 hard cap；响应 `usage.total_tokens` 计入 `metrics.record_deepseek(call_type, tokens_in, tokens_out)`（对齐 PRD §10 `deepseek_api_fail` 埋点）。
- 预算参考（一次完整拆解 ≈ input 1500 tokens + output 800 tokens ≈ **$0.00043**，全月 200 次拆解 ≈ $0.09）——写入 `reporting` 供用户知晓，不做独立成本监控（PRD §5.5 明确不做）。

---

## 4. 混合大脑路由（`router.py` + `sanitizer.py`）

### 4.1 路由决策表（单一真源）

| # | 场景 | 引擎 | 理由 | 降级 |
|---|---|---|---|---|
| R1 | 屏幕视觉判定（progress/stuck/off_track） | **本地 9B** | 截图不出本机（O-006/O-013） | 捕获失败 → UNKNOWN（沿用 V1） |
| R2 | 意图理解/澄清提取（I-1/I-2） | **本地 9B** | 轻量、隐私、零成本 | 本地 9B 不可用 → 直接透传用户原文为意图，标记 low_confidence |
| R3 | 会话摘要生成（I-1/I-3） | **本地 9B** | **隐私第一：摘要必须在本地生成，只上传摘要** | 本地 9B 不可用 → 不创建任务，提示监控降级 |
| R4 | 任务拆解（C-1） | **DeepSeek** | 强推理/规划，O-011 | 熔断/失败 → 本地 9B 简化拆解（≤3 步）+ 明示"降级"；仍失败 → 返回 40201 |
| R5 | 指令生成（C-2） | **DeepSeek** | 用户核心痛点"指令下不好"，需强模型 | 熔断/失败 → 用本地 9B 生成通用指令文本 + 明示降级 |
| R6 | 语义跑偏评审（§6） | **DeepSeek** | 语义级判断需强模型 | 熔断 → 跳过本轮评审，沿用视觉判定；`state` 不变更 |
| R7 | 跑偏/卡住提醒推送 | 本地规则（PushManager） | 即时、脱敏（P-1） | 推送 Provider 熔断 → 熔断逻辑已在 PushManager 内 |

路由入口：`route(call_type: str) -> str` 返回 `"local" | "deepseek"`，供各 service 调 `router.ensure_available()`；熔断/失败时按上表降级并 `emit` 降级事件。

### 4.2 隐私边界（O-006 延续，硬约束）

1. **截图永不上传**：视觉分析输入仅为本地 `tmp/captures` 帧文件，不进 DeepSeek 请求。
2. **上传仅脱敏摘要**：DeepSeek 收到的唯一会话内容是 `sanitizer.sanitize()` 处理后的摘要文本。
3. **webhook/API key 不入库**：`DEEPSEEK_API_KEY` 与 `WECOM_WEBHOOK_URL` 均只存 `.env`。

### 4.3 脱敏规则（`sanitizer.py`，上传前必经）

| 规则 | 匹配 | 替换为 |
|---|---|---|
| 文件/目录路径 | `[A-Za-z]:\\...`、`/home/...`、`/Users/...`、`/c/...` | `[路径]` |
| 代码片段 | 连续代码块（含 `def `、`{`、`;`、`=>` 等启发式，长度 ≥40 字符） | `[代码片段]` |
| API key/token | 32+ 位混合大小写数字串、`sk-`/`ghp_`/`AKIA` 前缀 | `[密钥]` |
| 长 token（含窗口标题噪音） | 连续无空格 60+ 字符 | `[长文本]` |
| 邮箱/手机号 | 标准正则 | `[联系方式]` |

- `sanitize(text) -> str` 为纯函数（可单测）；本地 9B 生成摘要后、DeepSeek 请求前各调用一次（双保险）。
- 单元测试用例集：**含路径/含代码/含 key/含邮箱/正常文本** 五类；断言替换结果不含原文子串（抽查 20 字符）。

---

## 5. 意图理解 → 指令拆解管线（`intent_service.py` + `task_service.py`）

### 5.1 管线总览（成功流）

```
用户文本/语音(V1.1) → POST /brain/intent
  → intent_service.extract（本地 9B）→ IntentExtract{intent_type, target_app, confidence, clarifying_questions[]}
  → 若 confidence < 阈值 → 追问澄清（最多 2 轮，I-2）→ 更新 intent
  → intent_service.build_summary（本地 9B）→ sanitizer → SessionSummary
  → task_service.create（存 BrainTask, status=intent_ready）
POST /brain/task {task_id}
  → task_service.decompose（DeepSeek）→ SubtaskList(3-8 步) → status=decomposed
  → task_service.instruct（DeepSeek）→ InstructionDraft → status=awaiting_confirm（C-2 预览）
  → reporting 推送"拆解完成"（脱敏）
POST /brain/inject {task_id, decision}
  → task_service.confirm_inject（N-1/N-2/N-3）→ injector.inject → status=injected|denied
```

### 5.2 JSON Schema（`schemas.py`，Pydantic 模型 + openapi 引用）

```yaml
IntentInput:            # POST /api/v1/brain/intent 请求体
  type: object
  required: [text]
  properties:
    text: {type: string, minLength: 2, maxLength: 2000, description: 用户自然语言意图（语音 V1.1 转文本后同入口）}
    source: {type: string, enum: [text, voice], default: text}
    target_app: {type: string, enum: [codex, trae, hermes, workbuddy], description: 可选，缺省由本地 9B 判定}
    session_id: {type: string, nullable: true, description: 关联的监控会话（可选）}

IntentExtract:
  type: object
  properties:
    intent_type: {type: string, enum: [refactor, implement, fix_bug, add_feature, optimize, test, explain, other]}
    target_app: {type: string, example: codex}
    confidence: {type: number, minimum: 0, maximum: 1, description: 本地 9B 意图置信度}
    clarifying_questions: {type: array, items: {type: string}, description: 需追问的问题（空 = 无需澄清）}
    sanitized_summary: {type: string, maxLength: 1200, description: 脱敏会话摘要（本地 9B 生成，截图不出本机）}

Subtask:
  type: object
  required: [id, goal, acceptance, rollback_hint, depends_on]
  properties:
    id: {type: string, example: "T1"}
    goal: {type: string, description: 单步目标（对 Codex 可执行）}
    acceptance: {type: array, items: {type: string}, description: 验收点（≥1）}
    rollback_hint: {type: string, description: 回滚提示}
    depends_on: {type: array, items: {type: string}, description: 依赖的前置子任务 id（空数组 = 无依赖；V1.5 不调度 DAG，仅顺序建议）}

SubtaskList:
  type: object
  required: [task_id, subtasks]
  properties:
    task_id: {type: string, example: "BT-20260803-001"}
    subtasks: {type: array, minItems: 3, maxItems: 8, items: {$ref: Subtask}}

InstructionDraft:
  type: object
  required: [task_id, instruction_text, preview]
  properties:
    task_id: {type: string}
    instruction_text: {type: string, maxLength: 6000, description: 对 Codex 最优指令文本（注入/粘贴内容）}
    preview: {type: string, description: 展示用预览（脱敏，≤200 字）}
    generated_via: {type: string, enum: [deepseek, local_fallback]}

ReviewVerdict:
  type: object
  required: [verdict, evidence]
  properties:
    verdict: {type: string, enum: [on_track, off_track]}
    evidence: {type: string, description: 依据（引用摘要事实，不含代码）}
    correction_suggestion: {type: string, nullable: true, description: off_track 时的修正建议（R6）}

BrainTask:              # GET /api/v1/brain/tasks 列表项 / 详情
  type: object
  required: [task_id, status, created_at]
  properties:
    task_id: {type: string, example: "BT-20260803-001"}
    status: {type: string, enum: [intent_ready, decomposed, awaiting_confirm, injected, denied, failed, expired]}
    intent: {$ref: IntentExtract}
    subtasks: {type: array, items: {$ref: Subtask}, nullable: true}
    instruction: {$ref: InstructionDraft, nullable: true}
    review: {$ref: ReviewVerdict, nullable: true}
    created_at: {type: number, format: unix-epoch}
    updated_at: {type: number, format: unix-epoch}
    error: {type: string, nullable: true}
```

### 5.3 状态机（`task_service.py` 唯一真源）

```
intent_ready ──decompose──► decomposed ──instruct──► awaiting_confirm ──confirm──► injected
      │                        │                          │                      │
      │                        │                          ├──deny──► denied      │
      │                        │                          └──timeout(300s)► expired
      └──失败──► failed（error 记录原因，可重试）
```

- 并发约束：每 task 一个 `asyncio.Lock`；`inject` 与 `instruct` 不得并发执行（防止先注入后生成）。
- 生命周期：`awaiting_confirm` 起 300s 无确认 → `expired`（报告提示"指令已过期，可重新生成"）。
- 重生成限频（C-3）：同 task `instruct` 重生成 ≤1 次/min；超限返回 42901。

### 5.4 Prompt 设计要点（写入 `config/prompts/*.md`，不散写）

- `decompose.md`：系统消息 = "你是软件工程任务拆解器。把用户意图拆成 3-8 步可执行子任务，每步含目标/验收点/回滚提示/依赖。输出 JSON 匹配给定 schema。**只基于提供的摘要，不得臆造代码或路径**。"用户消息 = 脱敏摘要 + 意图 + JSON schema。
- `instruct_codex.md`：系统消息 = "你是 Codex 指令优化器。用户痛点是自己不会下指令。把子任务清单组装成一段对 Codex 最有效的指令文本：明确约束、目标、验收标准、失败回滚。用中文，直接可粘贴。"
- 硬约束：两个 prompt 的输入**都必须已脱敏**；prompt 文件路径进 `config/brain.yaml`。

---

## 6. 语义级跑偏评审（`offtrack_reviewer.py`，V1.5 升级）

### 6.1 触发

| 触发 | 条件 | 频率 |
|---|---|---|
| 定时 | 监控循环每 `review.interval_frames`（默认 5）帧且存在 `awaiting_confirm` 之后的任务 | 约 30-40s 一次 |
| 里程碑 | 子任务状态变化 / 注入成功 / 视觉判定 off_track | 事件驱动 |

### 6.2 流程

1. 取当前 `BrainTask` 的**当前子任务目标**（未注入前取 `subtasks[0].goal`；注入后取最近注入指令对应目标）。
2. 取最近 N 帧（默认 6）视觉摘要 → 本地 9B 压缩为 ≤600 字**里程碑摘要** → sanitizer。
3. `DeepSeekClient.chat_json`（prompt=`review.md`）→ `ReviewVerdict`。
4. `off_track` → ① 更新 `task.review`；② 触发 `EVT_BRAIN_REVIEW`；③ reporting 推送脱敏跑偏警告（含 `correction_suggestion` ≤20 字）；④ 若该 app 处于监控中，将视觉 `alert_level` 提升至 3（对齐 D-3 严重度定级）。
5. `on_track` → 仅更新 `task.review`，不打扰。

### 6.3 与 V1 视觉判定的关系

- V1 视觉判定（3 帧 + 超时）**保留**，负责即时 stuck/off_track 提醒。
- V1.5 语义评审是**增量**：用 DeepSeek 判断"摘要内容 vs 目标语义是否一致"，补视觉判定对"改错方向但界面在动"的盲区（PRD §2.2 内容级跑偏检测）。两者独立、可同时触发。
- 评审失败（熔断/超时）→ 跳过本轮，不影响视觉判定与推送。

---

## 7. 确认后注入（`injector.py`，O-012 默认 + O-013 受控）

### 7.1 注入状态机（与 task 状态机协同）

```
generated(awaiting_confirm)
   ├─ 用户 confirm → injector.validate_focus() → 通过 → 注入 → injected
   │                                      └─ 不通过 → 40303（提示聚焦 Codex 输入框）
   ├─ 用户 deny → denied
   ├─ 超时 300s → expired
   └─ 备用通道：injector.write_fallback_file() → 用户手动粘贴 → 手动标记 injected
```

### 7.2 主通道：剪贴板 + win32 SendInput（受控注入，O-013）

```python
# backend/app/brain/injector.py
class Injector:
    def __init__(self, cfg: BrainConfig, bus: EventBus) -> None: ...

    async def validate_focus(self, target_app: str = "codex") -> InjectFocusResult
        # 校验前台窗口标题是否匹配 monitors.yaml 中 codex 的 window_title_regex
        # 仅标题匹配，绝不读取窗口内容（O-013 第二边界）
        # 返回 {ok, window_title(脱敏), reason}

    async def inject(self, task: BrainTask) -> InjectResult
        # 1. 仅注入 instruction_text 纯文本（不含其他命令）
        # 2. 写入剪贴板（win32clipboard.OpenClipboard/SetClipboardData）
        # 3. 延迟 150ms（剪贴板竞态规避，§11 已知坑）
        # 4. SendInput: Ctrl+V → 延迟 100ms → Enter
        # 5. 结果记录审计日志（§7.4）
        # 返回 {ok, channel: "sendinput", error}

    async def write_fallback_file(self, task: BrainTask) -> Path
        # 备用通道：写 backend/data/instructions/{task_id}.md，仅含指令文本
        # 用户手动粘贴后调用 POST /brain/inject {decision:"confirm", manual:true} 标记
```

- **不执行**：不读 Codex 内部数据、不模拟 Tab/鼠标点击/其他快捷键、不修改 Codex 配置——仅"聚焦校验 + 剪贴板 + Ctrl+V + Enter"。
- `win32` 依赖：`pywin32>=306` 已在 requirements.txt（windows-capture 同链）。
- 注入目标当前固定为 `codex`（O-008 重点目标）；多 harness 注入留 V2。

### 7.3 前端配合（桌宠确认面板）

- `awaiting_confirm` 时 WS 下发 `EVT_BRAIN_TASK`（含 `instruction.preview` + `has_fallback`），桌宠弹确认卡：显示预览 + "注入 Codex" / "拒绝" / "复制到文件"。
- 注入前前端提示"请将 Codex 输入框置于前台焦点"，用户确认后调 `POST /brain/inject`。
- 前端图标遵循 project 统一 SVG 图标库约束（design-alert-levels.md：无 emoji 作图标、无 hex 字面量），确认卡按钮用现有 SVG 组件，不新增图标依赖。

### 7.4 审计日志（N-3 留痕，硬约束）

`backend/data/inject_audit.jsonl` 每行：

```json
{"ts": 1780000000, "task_id": "BT-20260803-001", "target": "codex",
 "action": "inject|deny|fallback|expire", "result": "ok|fail|denied|timeout",
 "instruction_preview": "前 60 字摘要（不含指令全文）"}
```

- **不含指令全文**（指令文本本身可能含代码）；仅存预览摘要，避免日志堆积敏感内容。
- 写入原子（append + flush）；文件损坏容忍（追加失败仅记 warning 不阻断）。

---

## 8. 单向报告（`reporting.py`，V1.5 扩展）

### 8.1 报告事件 → 推送映射（复用 PushManager，P-1 脱敏）

| 事件 | 触发 | 推送文本模板（≤40 字） |
|---|---|---|
| 拆解完成 | `EVT_BRAIN_TASK(status=awaiting_confirm)` | `[拆解完成] {intent_type} → 3-8 步任务已生成，请到桌宠确认` |
| 跑偏警告 | `EVT_BRAIN_REVIEW(off_track)` | `[跑偏警告] {app_id}：{correction_suggestion 前 20 字}` |
| 注入结果 | `EVT_BRAIN_INJECT` | `[已注入] {app_id} 指令已发送 | [已拒绝] 用户未确认` |
| 降级提示 | `route()` 熔断/本地降级 | `[大脑降级] DeepSeek 不可用，当前为本地简化模式` |

- 推送格式对齐 P-1：`app_id + 状态 + ≤20 字摘要 + 建议`，**不附截图、不含代码、不含文件路径**。
- 调用 `PushManager.push(text, title="贾克斯 · {app_id}")`；推送失败由 PushManager 熔断/重试逻辑兜底（已有契约，不改）。
- 报告节流：同任务同事件类型 ≥60s 内不重复推送（防刷）。

---

## 9. API 契约（V1.5，与 openapi.yaml 风格一致）

> openapi.yaml 修订点（实施阶段同步，本 spec 为契约真源）：新增 `brain` paths + 上述 schemas；全部端点标注 `x-phase: v1.5`、`x-implemented: false`（未实现）。WS 契约新增 `brain_intent` / `brain_task` / `brain_inject` / `brain_review` 事件。

### 9.1 端点清单

```yaml
POST /api/v1/brain/intent
  summary: 意图输入（本地 9B 提取 → 脱敏摘要 → 建任务草稿）
  operationId: createBrainIntent
  x-phase: v1.5
  requestBody: {$ref: IntentInput}
  responses:
    "201": {description: 意图已受理（含澄清问题或摘要）, schema: {$ref: BrainTask}}
    "400": {description: 意图为空/超长 → 40001}
    "503": {description: 本地 9B 不可用（无法生成摘要，R3 不降级） → 50301}

POST /api/v1/brain/task
  summary: 拆解 + 指令生成（DeepSeek）→ awaiting_confirm
  operationId: decomposeBrainTask
  x-phase: v1.5
  requestBody: {required: [task_id], properties: {task_id: string, regenerate: bool(default false)}}
  responses:
    "200": {description: 拆解完成，含 SubtaskList + InstructionDraft, schema: {$ref: BrainTask}}
    "402": {description: DeepSeek 不可用（熔断/网络/认证失败） → 40201，含降级信息}
    "403": {description: task_id 不存在或状态不允许 → 40301/40302}
    "429": {description: 重生成超频（1 次/min） → 42901}

POST /api/v1/brain/inject
  summary: 注入确认（受控注入：confirm/deny）
  operationId: confirmBrainInject
  x-phase: v1.5
  requestBody:
    required: [task_id, decision]
    properties:
      task_id: {type: string}
      decision: {type: string, enum: [confirm, deny]}
      manual: {type: boolean, default: false, description: true=备用文件通道手动粘贴后确认}
  responses:
    "200": {description: 注入结果, schema: {type: object, properties: {status: enum[injected, denied], channel: enum[sendinput, fallback, none]}}}
    "403": {description: 40302 状态不允许 / 40303 聚焦校验失败（提示将 Codex 置前台）}
    "409": {description: 40901 注入进行中（幂等返回当前状态）}

GET /api/v1/brain/tasks
  summary: 任务列表（分页）
  operationId: listBrainTasks
  x-phase: v1.5
  parameters:
    - name: status, in: query, schema: enum[...], description: 按状态过滤（可选）
    - name: page, in: query, schema: {type: integer, default: 1}
    - name: limit, in: query, schema: {type: integer, default: 20, maximum: 50}
  responses:
    "200": {description: 分页列表, schema: {type: object, properties: {items: array(BrainTask), total, page, limit, hasMore}}}

GET /api/v1/brain/tasks/{task_id}
  summary: 任务详情（含子任务/指令/评审）
  operationId: getBrainTask
  x-phase: v1.5
  responses:
    "200": {schema: {$ref: BrainTask}}
    "403": {description: task_id 不存在 → 40301}
```

### 9.2 错误码（延续 4 位数字风格，统一 `ErrorBody`）

| 错误码 | 含义 |
|---|---|
| 40001 | 意图为空/超长/非法 source |
| 40201 | DeepSeek 不可用（熔断/网络/认证失败） |
| 40301 | task_id 不存在 |
| 40302 | 任务状态不允许该操作 |
| 40303 | 注入前置失败（目标窗口未聚焦/标题不匹配） |
| 40401 | app_id 未知（沿用） |
| 40901 | 注入进行中（幂等） |
| 42901 | 重生成超频 |
| 50301 | 服务未就绪（本地 9B 不可用等） |

---

## 10. 依赖与归属判断

| 项 | 归属 | 依赖 |
|---|---|---|
| `deepseek_client.py`（接口/超时/重试/熔断/token 预算） | **V1.5** | 无（结构 + 单测先行） |
| `router.py` 决策表 + `sanitizer.py` 脱敏 | **V1.5** | 无 |
| `intent_service.py`（本地 9B 意图提取 + 摘要） | **V1.5** | `llama_omni_client.chat`（SSE 已实现） |
| `task_service.py` 拆解/指令生成 | **V1.5** | DeepSeek 实测参数（§9 PoC 回填）；prompt 模板 |
| `injector.py` 注入 | **V1.5** | pywin32 已装；Codex 窗口标题匹配需对齐 monitors.yaml（B2 实测校准） |
| `offtrack_reviewer.py` 语义评审 | **V1.5** | DeepSeek；里程碑摘要本地压缩 |
| `reporting.py` 单向报告 | **V1.5** | PushManager（已有） |
| 多 harness DAG 编排 / 双向远程 / 全自动注入 | **out-of-scope-V1.5**（V2） | — |

---

## 11. 已知坑（内嵌硬约束，照做避开）

1. **代理坑（项目已踩）**：环境代理 127.0.0.1:7890 曾致外部请求失败。DeepSeek 客户端 `httpx.AsyncClient` **不隐式信任环境代理**（`trust_env` 显式关闭或透传配置），避免本地代理残留导致超时/误判网络故障。
2. **剪贴板竞态**：SetClipboardData 后立刻 SendInput 会偶发失败；注入序列必须含 150ms 延迟（§7.2 已含，勿删）。
3. **SSE 教训**：本地引擎已因 SSE 解析坑修复过（backend-llama-client-spec）；DeepSeek 客户端本轮用**非流式 JSON**（`stream=false`）规避同类问题；若未来需要流式，复用 `engine/sse.py` 模式而非新写。
4. **摘要即隐私边界**：任何进 DeepSeek 的文本必须过 `sanitizer`；本地 9B 生成摘要的 prompt 内不得携带截图路径。
5. **注入审计日志不存指令全文**（含代码），仅存预览（§7.4）。
6. **状态机并发**：task 级 `asyncio.Lock` 必须覆盖 `inject`+`instruct`；否则用户点确认瞬间与重生成并发会先注入旧指令。
7. **42901 防刷**：重生成限频是用户可感知的节流，不是安全机制；安全限流由网关/中间件统一负责（不在此重复实现）。
8. **`review` 输入必须短**：超过 600 字里程碑摘要的评审质量与成本双劣化；超长截断而非扩展。

---

## 12. 验收清单（照做）

- [ ] `DeepSeekClient.chat_json` 对 `deepseek-v4-flash` 返回合法 JSON；401/403 抛 `DeepSeekAuthError`（不重试）；网络错误重试 2 次退避正确；连续 3 次失败熔断 300s
- [ ] `sanitizer` 五类用例全绿：路径/代码/key/邮箱/长文本全部替换，输出不含原文 20 字符子串
- [ ] `route()` 决策表：R1-R7 各场景返回正确引擎；DeepSeek 熔断时 R4/R5 走本地降级并 `emit` 降级事件
- [ ] 意图管线：`POST /brain/intent` → 本地 9B 返回 `IntentExtract`（含 confidence/澄清问题）；低置信度追问 ≤2 轮
- [ ] 拆解管线：`POST /brain/task` → SubtaskList 3-8 步（含 goal/acceptance/rollback/depends_on）+ InstructionDraft；状态 `awaiting_confirm`
- [ ] 注入：未确认 → 状态 `awaiting_confirm` 且 Codex 输入框无内容（可测）；确认后注入成功（e2e 手动验证）；拒绝 → `denied`；聚焦失败 → 40303
- [ ] 语义评审：构造 off_track 场景（摘要与目标矛盾）→ ReviewVerdict=off_track + 修正建议；推送脱敏警告（不含代码/路径/截图）
- [ ] 单向报告：拆解完成/跑偏/注入结果推送内容满足 P-1 脱敏；同任务同事件 ≥60s 节流
- [ ] 隐私抓包：仅脱敏摘要上传 DeepSeek；`DEEPSEEK_API_KEY` 不出现在日志/JSON/DB
- [ ] 单测全绿 + `e2e_verify.py` 通过 + 回归率 0；`find backend/app/brain -name '*.py' | xargs wc -l` 最大文件 ≤300 行

---

## 13. 端到端验证（E2E，照做）

1. `.env` 配 `DEEPSEEK_API_KEY` → 启动后端 → `/health` 正常；`GET /api/v1/brain/tasks` 返回空列表。
2. `POST /api/v1/brain/intent` `{"text":"帮我重构项目的数据层，拆成接口+实现","target_app":"codex"}` → 返回 `BrainTask(status=intent_ready)`，含脱敏摘要（日志抓包确认无路径/无代码）。
3. `POST /api/v1/brain/task` `{"task_id":"<上一步>"}` → 返回 `subtasks`（3-8 步）+ `instruction.instruction_text`，状态 `awaiting_confirm`；同时手机收到"[拆解完成]"脱敏推送（若 webhook 已配）。
4. 打开 Codex 输入框（前台聚焦）→ `POST /api/v1/brain/inject` `{"task_id":"<...>","decision":"confirm"}` → 200，Codex 输入框出现指令文本并发送，状态 `injected`；`inject_audit.jsonl` 落一条（仅预览）。
5. 反向验证：不聚焦 Codex（切到其他窗口）→ `POST /brain/inject confirm` → 40303，Codex 无内容。
6. 熔断验证：临时改错 `DEEPSEEK_API_KEY` → `POST /brain/task` → 40201（或本地降级 + 提示），连续 3 次后 `/health` 返回 `deepseek=unavailable`，进程不崩；改回 key 冷却到期恢复。
7. 语义评审验证：注入后让 Codex 实际跑一个与目标相悖的改动 → 监控摘要与子任务目标矛盾 → 30-40s 内 `task.review.verdict=off_track` + 手机收到跑偏警告（脱敏）。
8. 回归：`cd backend && pytest -q` 全绿（含既有 105 用例 + 新增 brain 用例）。
