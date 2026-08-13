# ADR-023: 借 Hermes 的融合边界 —— 委派后端抽象 + A2A 协议 + 独立进程

## Status: Accepted (2026-08-13，首席架构师裁决，待项目总监复核)

> 本 ADR 裁决 `docs/master-roadmap.md` v2.1 §5 的三个决策点 J1/J2/J3，并给出
> 「语音意图 → 后台任务」的接口预留清单，让 M2/M3 产品化阶段提前把委派后端抽象
> 成「插槽」，M4 接 Hermes 是「插上去」而非「重做」。
>
> 编号说明：`docs/decisions/` 已到 ADR-022，本决策顺延为 **ADR-023**。
>
> 事实校正（先于裁决，避免在错误前提上拍板）：roadmap §5 J2 原建议「ACP 对齐
> Codex/Qwen-Audio-Agent」。经联网核验，**Hermes 支持的是 A2A 而非 ACP**，且二者
> 是方向相反的两个协议。J2 裁决据此从「ACP」修正为「A2A」，详见 D2。

## Background

### 竞争战略（已定，本 ADR 不改）

`docs/master-roadmap.md` v2.1 已确立：波斯猫 = 站在 Hermes 肩膀上的商业化移动语音
agent。后台 agent 引擎（Codex 半边）借 Hermes（开源、商用不设限、v0.20.0 已做到
实时语音+打断+唤醒词），语音前台（GPT-Live 半边）波斯猫自研端到端语音，护城河 =
商业化产品化（签名/隐私/杀毒零误报/可上架）。

三条红线：不 fork Hermes 二次改造；不自造通用 agent 引擎；自研范围锁定为「移动端
采集/双工链路 + 端到端模型接入 + 商业化产品化」。

### 已核验的 Hermes 事实（联网查证，非臆测）

1. **A2A v1.0 已支持**（v0.20.0 "The Herald Release"，2026-08-03）：以 bundled plugin
   `plugins/platforms/a2a/` 集成，纯 stdlib 实现、不依赖 a2a-sdk、**默认不启用**。
   A2A 是 Linux Foundation 标准（a2a-protocol.org，Google 牵头，v1.0 于 2026-03
   发布，150+ 组织背书）。Hermes 侧暴露三个工具：`a2a_discover`（读对方 Agent Card）、
   `a2a_call`（传任务给另一 agent，可多轮）、`a2a_orchestrate`（多 agent 分发聚合）。
2. **Hermes 既可当 caller 也可当 callee**：作为 A2A server 运行时，发布 Agent Card、
   接受外部任务，复用同一套 memory/toolset。server 默认只监听 localhost；对外需
   token + host 配置；内置限流（60 次/分/identifier）、round-trip 上限（5）、响应
   超时（300s）。
3. **signed outbound webhook**：v0.20.0 支持把 session activity / turn completion /
   tool completion 事件以 HMAC 签名推送到注册的 HTTP endpoint——这是「后台干活 →
   回报前台」的现成通道。
4. **ACP 是另一个方向**：ACP（Agent Client Protocol，Zed 提出，JSON-RPC 2.0 over
   stdio）= editor/前端 ↔ agent，Hermes 的 `acp_adapter/` 是让 VS Code/Zed/JetBrains
   把 Hermes 当「编辑器聊天后端」。它解决的是「人（编辑器）接 agent」，不是「agent
   委派 agent」。
5. **运行形态**：CLI / TUI / 桌面 app / gateway（多平台消息）/ **daemon 模式**
   （`hermes start` 后台守护，持久 memory.db + scheduler，暴露本地 HTTP API）。
   **Windows 原生支持为 early beta，官方建议 WSL2**。

### 已核验的现有代码事实（非二次侦察）

波斯猫后端**已经有一条完整的「意图 → 拆解 → 注入」管线** `backend/app/brain/`：

- `intent_service.py`：R2 意图提取（本地 9B）+ R3 脱敏摘要（本地 9B，隐私第一）。
- `task_service.py`：R4 拆解（DeepSeek）+ R5 指令生成（DeepSeek），本地简化降级。
- `pipeline.py::BrainPipeline`：状态机 `intent_ready → awaiting_confirm → injected`，
  `create_intent` / `decompose_task` / `confirm_inject` 唯一编排点。
- `injector.py::Injector`：**当前把结构化任务硬编码为「剪贴板 + SendInput 注入
  Codex GUI」**（`validate_focus` 校验窗口标题 → `inject` 粘指令 → `write_fallback_file`
  写文件兜底）。
- `schemas.py::BrainTask`：已是结构化契约（`intent` + `subtasks` + `instruction` +
  `confirm_token` + `session_id` + `status`），可直接映射到 A2A Message。
- `router.py`：R1–R7 混合大脑决策表，R4/R5 走 DeepSeek、R2/R3 走本地。
- `config.py::InjectConfig`（`config/brain.yaml:34-42`）：`target_app: codex`、
  `instructions_dir`、`audit_path`。装配点 `main.py:140`：`Injector(app_config.brain,
  app_config.monitors)`。
- `config/monitors.yaml:23-28`：`hermes` 目前只被登记为「**被截图监控的窗口**」
  （`app_id: hermes`、`window_title_regex: (?i)hermes`），与 codex/trae 同列。

关键洞察：**现有 brain 管线的「注入」这一步（`Injector`）是「把 Codex 当 GUI 程序
驱动」，这正是借 Hermes 时要替换的缝。把 `Injector` 抽象成 `AgentBackend` 协议，就
是「插上去」的接口位。** 同时 `monitors.yaml` 里 hermes 的语义要从「被监控窗口」
升级为「被委派的后台 agent」。

## Decision

### D1. J1 边界裁决：借 agent 引擎（选项 A），经「进程边界 + A2A」接入，不 fork / 不换皮 / 不自研

**裁决：选项 A —— 借 Hermes 的 agent 引擎（core/agent + 工具调用 + 技能 + 记忆 +
委派/子代理），GUI/CLI/产品化波斯猫自研。接入方式是「独立进程 + A2A 协议」，不是
「fork 源码」。**

- **借什么**：Hermes 的**执行能力**——工具调用（terminal/file/web/browser/vision/cron/
  自定义 skill）、技能系统（SKILL.md）、持久记忆（memory.db）、子代理委派
  （delegate_task）、调度（cron）。这些是 Hermes 的强项、自研成本最高，是「后台
  agent 引擎」的本体。
- **怎么借（与 fork 的本质区别）**：通过 Hermes **官方提供的进程/协议边界**调用——
  把 Hermes 作为独立后台服务（daemon 或 A2A server）被波斯猫委派，Hermes 是黑盒，
  波斯猫不碰它的源码、不碰它的生命周期、不碰它的升级节奏。fork = 拉源码进自己仓库
  改，红线禁止的是后者，本决策不涉及。
- **不借 GUI/Studio（选项 B 否）**：Hermes 桌面 GUI 是「开源开发者工具」，场景是
  桌面 PC 麦克风（sounddevice 采本机音）；波斯猫前台是「移动端 App + 桌宠 sidecar +
  pet-ui + 端到端 MiniCPM-o」。换皮无法复用移动端差异化，还会被 Hermes 桌面 app 的
  发布节奏绑架。GUI/产品化是波斯猫护城河，必须自研。
- **不自研引擎（选项 C 否）**：只借 A2A 协议规范、引擎自研 = 重复造工具/技能/记忆/
  委派，违反红线「不自造通用 agent 引擎」，且追不上 Hermes 每两周一个命名版本的节奏。
- **边界细化（波斯猫保留 vs 借 Hermes）**：
  - 波斯猫保留：语音前台（采集/双工/端到端模型）、意图理解 R2 + 脱敏摘要 R3（本地
    9B，隐私第一，绝不把原始语音/文本外传）、会话管理、回报推送、商业化产品化。
  - 借 Hermes：R4/R5 之后的**执行**——拆解落地、工具调用、技能、记忆、委派。
  - R4/R5（DeepSeek 拆解/指令生成）的去留**本 ADR 不裁决**：Hermes 自有 agent 引擎
    可自行拆解执行，届时 R4/R5 可能退化为「波斯猫只产出意图摘要，Hermes 内部拆解」。
    这是 M4 融合实现期的决策，本 ADR 只保证「委派抽象」不把 R4/R5 焊死。

### D2. J2 协议裁决：A2A v1.0（校正 ACP 误判），内部 IPC 作 MVP 过渡

**裁决：语音前台 → 后台 agent 的委派用 A2A v1.0；内部 IPC 降为「MVP 过渡/降级」，
不是终态契约。**

- **为什么是 A2A 不是 ACP**：波斯猫「语音前台产出结构化意图 → 交给 Hermes 执行」是
  **agent-to-agent** 委派，不是 editor-to-agent。A2A 是 agent↔agent 协议（Hermes 已
  原生支持），ACP 是 editor↔agent 协议（Hermes 的 `acp_adapter/` 是给 VS Code/Zed 用）。
  用错协议 = 用一个「编辑器接入」协议去做「agent 委派」，语义和治理都不匹配。
- **复用 Hermes 原生能力**：A2A 自带 `discover/call/orchestrate` 三件套 + Agent Card
  发现 + token 鉴权 + 限流（60/min）+ 超时（300s）+ round-trip 上限（5）。这些治理
  语义正是委派闭环需要的，自研 IPC 要重造。
- **网络协议才能跨边界**：Hermes Windows 原生是 early beta、官方建议 WSL2。HTTP/JSON
  的 A2A 能跨「本机独立进程 / WSL2」边界，本地管道/共享内存跨不了 WSL。这正是 J3 选
  「独立进程」后协议层的配套结论。
- **内部 IPC 的定位**：在 A2A adapter 尚未实现（M2/M3）期间，委派暂走现有通道
  （Codex GUI 剪贴板注入，或 Hermes daemon 本地 HTTP API 直连）。A2A 是终态契约，
  本地 daemon API 是过渡实现。二者都藏在 `AgentBackend` 抽象后面，对 pipeline 透明。

### D3. J3 进程托管裁决：独立进程 + 网络协议，不子进程托管

**裁决：选项 A —— Hermes 独立进程，波斯猫经网络协议（A2A over HTTP）通信；不 fork、
不用宿主拉起 Hermes（不做子进程托管）。**

- 红线「不 fork」→ 波斯猫不把 Hermes 当子进程拉起、不注入/不接管它的生命周期。Hermes
  有自己的 `hermes start/stop`（daemon）、自己的内存库（`~/.hermes/`）、自己的升级
  节奏。
- 与现有架构一致：ADR-017 已确立「Tauri externalBin 只监督 sidecar」的独立进程模式；
  backend（FastAPI :8000）/ sidecar（Electron）/ rtc_bridge（:19092）/ llama-omni
  （:19080）都是独立进程 + loopback 通信。Hermes 是这条进程链上「又一个独立对等方」。
- 子进程托管（选项 B）引入「宿主拉起 Hermes」的耦合：崩溃/升级/退出语义都要宿主
  处理，且违背「Hermes 是独立后台服务」的定位。

### D4. 终态进程拓扑与数据流

```
波斯猫宿主（jax-pet.exe / Tauri）
├─ pet-ui（React 前台 UI） ── wss ──┐
├─ sidecar（Electron RTC/音频） ── ws ── rtc_bridge ── MiniCPM-o（端到端语音）
└─ backend（FastAPI :8000）
    └─ brain（意图→委派）
         └─ AgentBackend 抽象
              ├─ ClipboardInjector（codex_gui，过渡）
              └─ HermesA2ABackend ──A2A(HTTP/JSON)──► Hermes 独立进程
Hermes 独立进程（daemon / A2A server，可能在 WSL2）
├─ hermes start（daemon：memory.db + scheduler + 本地 API）
└─ A2A server（plugins/platforms/a2a，localhost + token）
     └─ signed webhook（HMAC）──► backend POST /api/v1/brain/events
```

委派闭环数据流（H3）：

```text
语音前台（MiniCPM-o 端到端）产出自然语言意图
  → backend brain.create_intent（R2/R3 本地意图提取 + 脱敏摘要，隐私第一）
  → BrainTask(status=intent_ready, session_id=…)
  → 用户确认 → confirm_inject → AgentBackend.delegate(task)
       ├─ codex_gui：剪贴板注入（现有，过渡）
       └─ hermes_a2a：HermesA2ABackend 把 BrainTask → A2A message（带 metadata.session_id）
                      → a2a_call → Hermes 独立进程执行（工具/技能/记忆/委派）
  → Hermes 完成 → signed webhook（turn_completion，HMAC）
  → backend /api/v1/brain/events 校验 HMAC → 按 remote_task_id 关联 BrainTask
  → 更新状态 + emit EVT_BRAIN_TASK → pet-ui WS 推送 + 语音前台 TTS 回报
```

## 接口预留清单（M2/M3 必做，精确到文件/接口形状）

核心原则：**只做「抽象 + 路由」，不实现 HermesA2ABackend。** M2/M3 交付后，M4 接
Hermes = 新增一个 `AgentBackend` 实现类 + 切配置，`pipeline.py` / `schemas.py` /
`routes_brain.py` 零改动。

### P1. 委派后端抽象（最关键接口位）

**文件**：`backend/app/brain/backends/__init__.py`（新建包）

```python
from typing import Protocol
from ..schemas import BrainTask

class DelegateResult:
    ok: bool
    channel: str              # "clipboard" | "a2a" | "fallback_file"
    remote_task_id: str | None  # A2A 返回的 task/context id，用于回报关联

class AgentBackend(Protocol):
    backend_id: str           # "codex_gui" | "hermes_a2a"
    async def delegate(self, task: BrainTask) -> DelegateResult: ...
```

**迁移**：`injector.py::Injector` 改名为 `backends/clipboard.py::ClipboardInjector`，
实现 `AgentBackend`，`delegate()` 内部复用现有 `validate_focus` + `inject` +
`write_fallback_file` 逻辑，`channel` 返回 `clipboard`/`fallback_file`。

**装配**（`main.py:140` 位置）：按 `config.brain.delegate.backend` 选择实现，注入
`BrainPipeline`。`BrainPipeline.__init__` 的 `injector: Injector` 参数改为
`backend: AgentBackend`。

### P2. 任务模型扩展（向后兼容）

**文件**：`backend/app/brain/schemas.py::BrainTask`

```python
class BrainTask(BaseModel):
    # ...现有字段不变...
    delegate_backend: Literal["codex_gui", "hermes_a2a"] = "codex_gui"
    remote_task_id: str | None = None    # A2A 回报关联键
```

`IntentInput` 已有 `target_app`（`codex/trae/hermes/workbuddy`），其语义当前是「注入
哪个 GUI 窗口」（`injector.validate_focus` 用）。**不复用 `target_app` 表示委派后端**，
新增 `delegate_backend` 区分「被监控窗口」与「被委派后端」两个正交维度。

### P3. 回报通道（Hermes → 波斯猫）

**文件**：`backend/app/api/routes_brain_events.py`（新建，注册进 `main.py`）

```
POST /api/v1/brain/events     （接收 Hermes signed webhook，HMAC 校验）
请求头：X-Hermes-Signature: <hmac_sha256>
请求体（Hermes webhook 事件）：
  { "event": "turn_completion" | "tool_completion" | "session_activity",
    "remote_task_id": "...", "payload": {...} }
```

处理链路：校验 HMAC（`webhook_secret`，fail-closed）→ 按 `remote_task_id` 定位
`BrainTask` → 更新状态（`injected` 后追加 `completed`，或记 `failed`）→ emit
`EVT_BRAIN_TASK` → 经现有 `routes_ws` 推送到 pet-ui + 语音前台 TTS 回报。

**会话级关联**：`BrainTask.session_id` 已存在，贯穿「语音会话 → BrainTask → A2A
task」；`HermesA2ABackend.delegate` 把 `session_id` 写入 A2A message 的
`metadata.session_id`，回报时凭 `remote_task_id` + `session_id` 双键定位是哪一次
语音问话，保证「问话 → 干活 → 回报」闭环可关联。

### P4. 配置层

**文件**：`backend/app/config.py`（新增 `DelegateConfig`，挂 `BrainConfig.delegate`）
与 `config/brain.yaml`：

```yaml
delegate:
  backend: codex_gui              # 当前阶段默认；M4 切 hermes_a2a
  hermes_a2a:
    base_url: http://127.0.0.1:7777        # Hermes A2A server / daemon API（以官方为准）
    agent_card_url: http://127.0.0.1:7777/.well-known/agent.json
    token: ""                     # A2A server 鉴权 token，仅存 .env，禁止入库
    webhook_secret: ""            # HMAC 签名密钥，仅存 .env，禁止入库
    timeout_s: 300                # 对齐 Hermes A2A 响应超时
    max_roundtrips: 5             # 对齐 Hermes A2A round-trip 上限
```

### P5. 依赖方向约束（代码组织规范）

- `pipeline.py` 只依赖 `backends.AgentBackend`（抽象），不 import 具体实现。
- `backends/hermes_a2a.py` 是 M4 交付物，M2/M3 只留文件占位 + 接口 stub
  （`delegate()` 抛 `NotImplementedError` 或返回 `ok=False, channel="a2a"`），
  保证抽象层先落地、不提前写未验证的 A2A 客户端。
- 单文件 ≤300 行：`clipboard.py` 从 `injector.py` 迁入时若超 300 行，把 Win32 底层
  （`_set_clipboard_text` / `_send_ctrl_v_enter` / `_foreground_window_title`）拆到
  `backends/_win32.py`。

### P6. M2/M3 的验收证据（不写 Hermes 也能验证抽象层就位）

1. `AgentBackend` Protocol 存在，`ClipboardInjector` 实现它，现有 `/api/v1/brain/*
   ` 端到端行为不变（回归测试通过）。
2. `BrainTask` 含 `delegate_backend` / `remote_task_id` 且默认值保持 `codex_gui`
   旧行为（存量 `brain_tasks.json` 反序列化不报错）。
3. `routes_brain_events.py` 的 HMAC 校验可被 curl + 伪造签名验证 fail-closed。
4. `config/brain.yaml` 含 `delegate` 段，`config.py` 能解析，缺省不破坏现有启动。

## Options considered

### J1 借 Hermes 的边界

| 维度 | A 只借 agent 引擎（**采用**） | B 连 GUI/Studio 一起借 | C 只借协议、引擎自研 |
|---|---|---|---|
| 红线「不 fork」 | 满足（黑盒服务调用） | 满足但换皮 | 满足 |
| 红线「不自造引擎」 | 满足 | 满足 | **违反** |
| 移动端差异化（护城河） | 保留（前台自研） | **丢失**（套 Hermes 桌面壳） | 保留但引擎自研成本高 |
| 追 Hermes 更新节奏 | 借进程边界，随 `hermes update` | 被桌面 app 发布节奏绑架 | 自研永远在追 |
| 商业化产品化自主权 | 完全自主 | 部分受 Hermes GUI 约束 | 完全自主 |

结论：A 是唯一同时满足两条红线、又保住移动端差异化与商业化自主权的方案。

### J2 委派协议

| 维度 | A2A v1.0（**采用**） | ACP | 内部 IPC |
|---|---|---|---|
| 协议方向 | agent ↔ agent（匹配委派） | editor ↔ agent（**不匹配**） | 无标准 |
| Hermes 原生支持 | 是（v0.20.0 plugin） | 是（acp_adapter，但方向错） | — |
| 治理语义（鉴权/限流/超时/round-trip） | 内置 | 部分 | 需自研 |
| 跨 WSL2 / 独立进程 | 是（HTTP） | 是（stdio，**跨不了网络**） | 本地管道跨不了 WSL |
| 生态/未来兼容 | Linux Foundation 标准，150+ 组织 | Zed 生态，编辑器向 | 私有 |

结论：A2A 与「agent 委派」语义天然匹配且 Hermes 已支持；ACP 是编辑器协议，方向错；
内部 IPC 只作 M2/M3 过渡，不是终态契约。

### J3 进程托管

| 维度 | 独立进程 + 网络协议（**采用**） | 子进程托管 |
|---|---|---|
| 红线「不 fork」 | 满足（不碰 Hermes 生命周期） | 边界模糊（宿主接管启动/退出） |
| Hermes 升级/崩溃/退出语义 | Hermes 自理（daemon 自愈） | 宿主需处理 |
| 与现有架构一致性 | 一致（ADR-017 独立进程模式） | 新增宿主耦合 |
| 跨 WSL2 | 是（网络协议） | 子进程托管跨不了 WSL |

## Consequences

正面后果：

- **防返工**：M2/M3 把 `Injector` 抽象成 `AgentBackend`，M4 接 Hermes 只是新增
  `HermesA2ABackend` 实现类 + 切配置，`pipeline`/`schemas`/`routes` 零改动。
- **协议选型对齐事实**：用 Hermes 已支持的 A2A（而非 roadmap 误写的 ACP），复用
  discover/call/orchestrate + 鉴权/限流/超时，不自研协议。
- **不 fork、不自造引擎**两条红线同时满足；Hermes 作为黑盒后台服务，随 `hermes
  update` 自动升级，波斯猫不被上游源码绑架。
- **跨 WSL2 就绪**：Windows 原生 early beta 的 Hermes 可跑在 WSL2，A2A 网络协议天然
  跨越该边界，不阻塞 M4。
- **回报闭环有现成通道**：Hermes signed webhook（HMAC）直接对接 `/api/v1/brain/events`，
  无需自建轮询。

负面后果：

- `monitors.yaml` 里 hermes 的「被监控窗口」语义要升级为「被委派后台」，M4 需清理
  这条监控条目（避免「又截图监控、又 A2A 委派」的双重身份歧义）。
- A2A 是「请求-响应」模型，带响应超时（300s）与 round-trip 上限（5）；长任务/流式
  进度要依赖 webhook 事件推送，不是 A2A 单次响应，回报通道（P3）因此是闭环的必需件
  而非可选件。
- 多一个独立进程（Hermes daemon）+ 一个密钥（webhook_secret / a2a token）要管理，
  卸载清理（D 线）需覆盖 Hermes 进程与 `~/.hermes/` 数据目录。
- M2/M3 只落地抽象层、不实现 `hermes_a2a.py`，会有一个「接口已预留但不可用」的窗口
  期；需在 UI/文档明示「后台 agent 委派待 M4 启用」，避免被误读为已上线功能。

## Migration and rollback

- M2/M3：`Injector` → `ClipboardInjector`（行为不变）→ 提炼 `AgentBackend` →
  `delegate_backend` 默认 `codex_gui` → 回归测试证明端到端行为不变。
- M4：新增 `backends/hermes_a2a.py` + `config.delegate.backend: hermes_a2a` 切流；
  首次用 `a2a_discover` 拉 Hermes Agent Card 验证连通，再开 `a2a_call` 委派。
- 回滚：`config.delegate.backend` 切回 `codex_gui` 即回到旧通道；`hermes_a2a.py`
  不加载不影响现有路径（装配期按配置选择）。
- 禁止回滚到「把 hermes 重新当截图监控窗口」后仍宣称已满足本 ADR 的委派闭环。

## Explicitly not doing

- 不 fork Hermes 源码、不把 Hermes 作为 Tauri externalBin 子进程托管拉起。
- 不自研通用 agent 引擎、不重复造工具/技能/记忆/委派。
- 不借 Hermes 桌面 GUI/Studio 换皮。
- 不用 ACP 做 agent 委派（方向错误）；不用本地管道/共享内存做终态协议（跨不了 WSL）。
- M2/M3 不实现 `hermes_a2a.py` 的真实 A2A 客户端（只留 stub + 配置），避免未验证代码
  提前入库。
- 不在本 ADR 裁决 R4/R5（DeepSeek 拆解/指令）在借 Hermes 后的去留——留待 M4 实现期。

## Design-discipline references

- `spec-as-contract.md`：以已核验的代码事实（`injector.py` 硬编码剪贴板注入、
  `monitors.yaml` 把 hermes 当监控窗口、`main.py:140` 装配点）为契约，点名文件/函数/
  接口形状，不凭空新增机制。
- `context-engineering.md`：校正 roadmap §5 J2 的「ACP 对齐 Codex/Qwen-Audio-Agent」
  前提——Hermes 支持 A2A 而非 ACP，二者方向相反——把事实校正写进 D2 而非沉默迁就。
- `generated-code-failure-modes.md`：不把「Hermes 有 A2A」推断为「波斯猫可直接调」，
  通过 `AgentBackend` 抽象把「未验证的 A2A 客户端」隔离为 M4 交付物，M2/M3 不提前
  落未验证代码。

## Related ADRs

ADR-012（TRTC 传输）、ADR-013（商业 RTC 主路径）、ADR-017（Tauri externalBin 只监督
sidecar，独立进程模式）、ADR-018（本地最小隐私数据）、ADR-020（TLS 四端，回环承载）、
ADR-021（隐私开关，本 ADR 的委派链路复用在 R2/R3 本地脱敏之后）。
