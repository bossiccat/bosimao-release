# Hermes 委派闭环交互设计契约（草案）

> 状态：Phase 2 技术设计草案，供架构/产品确认后进入实现。  
> 范围：语音前台、Android App、桌宠 pet-ui 与 backend brain 的状态呈现；不修改 Android、pet-ui 或 backend 业务代码。  
> 设计寄存器：**Product**（工具型 UI，赢得 Linear / Notion / Raycast 熟手的熟悉感）。  
> 平台轴：Android + Web/桌宠（跨端状态契约）；Android 触摸目标按 Material 48dp，Web 交互目标按 44px。  
> 三轴刻度：`DESIGN_VARIANCE=4` / `MOTION_INTENSITY=3` / `VISUAL_DENSITY=5`。

## 1. 设计边界与已知事实

### 1.1 本稿解决的问题

当前的“连接中 / 已连接 / 监听中”是 TRTC、常驻监听服务和语音会话的事实，不能被用户理解为后台任务正在执行。此稿为后台委派增加独立的任务事实层，同时保留语音前台的即时感与桌宠的低打扰特性。

交互目标：

- 用户说出低风险任务后，一次受理流程自动派发，不需要反复点击“立即对话”。
- 高风险动作在执行前明确说明动作、目标和风险，得到与当前任务版本绑定的语音确认后才执行。
- 任务在 App、sidecar、backend 或 Hermes 重启/断线后仍显示持久化事实，不伪报完成、不重复执行、不重复播报。
- 桌宠显示“现在发生了什么”，手机显示“我能做什么”，语音播报只读“下一步最重要的事”。

### 1.2 从现有代码读到的事实

- 业务任务最小状态来自 `requirements.md` §FR-7：`intent_ready / clarify / awaiting_confirm / queued / running / needs_input / completed / failed / cancelled / denied / expired / recovering`。
- Android 新模型 `VoiceUiModel.ExperienceState` 有 10 个体验状态，旧 `VoiceUiState` 仍包含 `phase / connection / service` 三个维度（`VoiceUiModel.kt:6-10`、`VoiceState.kt:21-32`）。
- `VoiceForegroundService.renderModel` 当前将会话协调器状态渲染为连接/语音体验（`VoiceForegroundService.kt:208-230`），这是语音会话事实，不是后台任务事实。
- `pet-ui/src/App.tsx` 当前消费 `session_updated / alert / pet_state`，并通过 `wsClient` 订阅；代码中没有假设已存在可消费的 `brain_task` 事件。
- `backend/app/core/events.py` 已有 `EVT_BRAIN_INTENT / EVT_BRAIN_TASK / EVT_BRAIN_INJECT` 常量，但 `routes_ws.py` 当前 `WsHub` 仅订阅并广播会话、提醒和授权事件。因此，任务 WS 消费必须以 backend 先完成事件广播扩展为前置条件。
- ADR-023 规定 Hermes 是独立进程、A2A over HTTP；Hermes 的截图监控条目与“被委派后台 agent”是两个概念，UI 不应把二者合并成一个连接徽标。

## 2. 四层状态分离：事实枚举 vs 展示文案

### 2.1 四层事实模型（必须正交）

| 事实层 | 枚举/来源 | 用户可见的最短说明 | 禁止的混用 |
|---|---|---|---|
| **后台任务** | `intent_ready, clarify, awaiting_confirm, queued, running, needs_input, completed, failed, cancelled, denied, expired, recovering` | “任务：执行中”“任务：等待确认”等 | 不得由 TRTC `CONNECTED` 推断 `running` |
| **TRTC 连接** | `DISCONNECTED, CONNECTING, CONNECTED`（旧 `VoiceState.ConnectionState`） | “语音链路：未连接/连接中/已连接” | 不得写成“任务执行中” |
| **监听服务** | `STOPPED, RUNNING`（旧 `ServiceState`）+ 唤醒开关 | “监听服务：运行中/已暂停” | 不得写成“后台任务已受理” |
| **语音会话** | `ExperienceState` 10 态 / `VoiceSessionState` 生命周期 | “正在听/思考/播报”等 | 不得覆盖或改变后台任务终态 |

**强制规则：** UI 数据模型中保留四个命名空间（如 `task.status`、`trtc.connection`、`listener.service`、`voice.experience`），不要把四层压成一个 `status` 字段。展示层文案是可本地化的字符串，不能被其他层反向解析为事实。

### 2.2 事实枚举与展示层的界线

- **事实枚举**由 backend 持久化、版本化和幂等更新；它决定可执行动作和终态。
- **展示文案**由客户端根据枚举、风险、进度和错误分类生成；文案变更不应导致状态迁移。
- “已连接”只表示 TRTC 控制链路已建立；“监听中”只表示前台服务与麦克风管线运行；“执行中”只允许来自任务事实 `running`。
- `pet_state` 的 `listening/thinking/speaking` 是语音体验，不是任务事实。任务在 `running` 时语音也可以处于 `IDLE`，反之语音 `THINKING` 也不代表任务已派发。

### 2.3 推荐统一任务事件包（待 backend 契约确认）

```text
{
  "event_id": "稳定事件 ID",
  "event": "brain_task",
  "task_id": "内部关联 ID",
  "session_id": "语音会话关联 ID，可为空",
  "status": "事实枚举",
  "version": 7,
  "occurred_at": "ISO-8601",
  "summary": "脱敏的用户摘要",
  "progress": {"current": 2, "total": 4, "label": "正在汇总测试结果"},
  "risk": "low|high|blocked",
  "requires_action": "confirm|provide_input|retry|cancel|none",
  "error_class": "network|auth|permission|protocol|validation|unknown|null",
  "result_summary": "短结果摘要，可为空",
  "replay": false
}
```

`task_id/event_id/version` 用于幂等与迟到事件丢弃；`summary/result_summary` 必须脱敏，不能含 token、secret、原始日志或完整路径。

## 3. 用户可感知状态与文案

以下文案是展示层建议，不是新的事实枚举。语音版本控制在一句，桌宠/手机可显示第二句辅助说明。

| 事实状态 | 触发/含义 | 桌宠徽标与手机标题 | 语音播报建议 | 可执行动作 |
|---|---|---|---|---|
| `intent_ready` | 已生成草稿，尚未进入执行门禁 | “已整理任务” / “任务草稿已准备” | “我已整理好这个任务，正在判断是否需要确认。” | 查看摘要、取消 |
| `clarify` | 目标、范围或输入不足，不调用 Hermes | “需要补充信息” | “还缺少一个信息：请告诉我要检查哪个目录。” | 回答问题、取消 |
| `awaiting_confirm` | 高风险任务等待本次版本的明确确认 | “等待确认” | “将修改生产配置并重启服务，影响范围是 X。确认执行吗？” | 确认执行、拒绝、查看详情 |
| `queued` | 已受理，等待后端/Hermes 调度 | “排队中” | “任务已受理，正在排队。” | 取消（若可取消）、查看详情 |
| `running` | Hermes/后端已有执行事实 | “执行中”+可选进度 | “任务正在处理，当前在汇总测试结果。” | 取消（仅可取消阶段）、查看详情 |
| `needs_input` | 执行方需要用户补充信息/选择 | “需要你的输入” | “任务需要一个选择才能继续：请提供目标分支。” | 提供信息、取消 |
| `completed` | 已收到可验证完成事实 | “已完成” | “任务已完成。结果是：已整理出 3 项失败测试。” | 查看结果、再次执行（新任务） |
| `failed` | 不可自动完成或重试耗尽 | “执行失败” | “任务没有完成，原因是权限不足。可以查看详情或重试。” | 查看原因、重试（新版本）、取消/关闭 |
| `cancelled` | 用户取消成功，或系统确认取消事实 | “已取消” | “任务已取消。” | 查看时间线、再次执行（新任务） |
| `denied` | 安全边界/能力范围拒绝，未调用 Hermes | “未执行” | “这个请求无法执行，因为它超出安全范围。” | 查看原因、修改请求 |
| `expired` | 高风险确认超时/旧版本失效，未调用 Hermes | “确认已过期” | “确认已过期，任务没有执行。” | 重新查看并确认（新版本）、取消 |
| `recovering` | 重启/断线后暂时无法确认远端事实 | “正在恢复状态” | “我正在恢复任务状态，暂时不会重复执行。” | 等待、查看详情、联系人工 |

### 3.1 路由提示（非任务状态）

- `direct_reply`：直接回答，不创建任务；不显示任务徽标。
- `delegate + low`：先显示 `intent_ready` 的极短过渡，再在同一受理流程进入 `queued/running`；用户无需点击。
- `delegate + high`：进入 `awaiting_confirm`，未得到明确确认时不得进入 `queued/running`。
- `clarify` 与 `deny` 必须在 UI 上明确“未执行”，避免用户以为已派发。

### 3.2 拒绝、过期与失败的区分

- **拒绝 `denied`**：系统基于安全/能力边界主动不执行；没有 Hermes 调用。
- **过期 `expired`**：确认门禁失效（超时、任务版本/目标变化）；没有 Hermes 调用。
- **失败 `failed`**：已经尝试执行或无法完成，显示错误分类及下一步；不把“连接失败”写成“任务失败”除非任务事实确已失败。

## 4. 三种呈现表面

### 4.1 桌宠（低打扰状态锚点）

桌宠默认只呈现**当前最重要的一条任务**，不新增大型控制面板，不改变现有宠物/语音球主形态。

- 默认：桌宠旁 16px 描边状态图标 + 短标签（如“执行中”“需要确认”）。
- `awaiting_confirm / needs_input / failed`：显示轻量提醒点或可聚焦徽标；只在需要用户动作时提升提醒等级。
- `running / queued / recovering`：低频状态环或静态徽标，禁止持续闪烁；进度可在展开面板查看。
- `completed`：短暂显示完成徽标，随后进入一次性回报队列，不持续打扰。
- 展开已有 `MonitorPanel` 时，增加“任务”分组/入口；不要把 Hermes 状态塞进 `session_updated` 的连接卡。
- 桌宠的 `ConnectionBadge` 继续展示 TRTC/控制面事实，旁边单独放 `TaskBadge`，二者必须有不同 `aria-label`。

### 4.2 手机 App（可操作任务表面）

手机不要求用户再次点击“立即对话”才能查看或处理后台任务。推荐在现有语音页面的会话区域下方提供可收起的“后台任务”入口；任务列表/详情为普通信息页或 bottom sheet，不做新控制台。

- 顶部保留语音体验状态；任务入口显示未读终态数量和需要用户动作的数量。
- `awaiting_confirm` / `needs_input` 打开后直接定位到唯一主操作；确认按钮文案必须是具体动作（“确认修改并重启”），不能使用含糊的“继续”。
- 任务详情显示摘要、风险、当前事实、最近更新时间和时间线；敏感内容脱敏。
- 任务结果使用折叠区，默认先看 1–3 句摘要，再由用户主动展开细节。
- 手机的返回键/系统返回手势保持可预测：从详情返回列表，不触发取消任务。

### 4.3 语音播报（一次一事）

语音只播报对用户决策有用的信息，不朗读完整日志、内部 ID 或密钥。

- 受理：低风险自动派发时播报一次“已受理，正在处理”，不要求点击。
- 高风险：只在进入 `awaiting_confirm` 时播报动作+范围+风险，并等待明确肯定语句。
- 中间进度：默认不逐事件播报；仅在用户主动询问、任务长时间无变化且产品策略允许时播报一次摘要。
- 终态：`completed/failed/cancelled/denied/expired` 进入待播报队列；下一次语音会话开场播报一条摘要并标记已读。
- 同一任务同时有多个更新时合并为最新事实，不能把 `queued → running → completed` 全部排队朗读。
- 用户说“退下/停止播报”只停止当前语音输出，不改变后台任务事实；取消任务必须使用明确“取消这个任务”。

## 5. 状态徽标、图标与可访问性

### 5.1 图标语义

项目尚未在 Spec 中锁定具体图标库，**图标库是架构决策待定（OPEN）**。一旦锁定，全项目仅使用这一套统一描边 SVG 图标；不得使用 emoji 作为功能图标。尺寸固定：行内 16px、按钮内 20px、独立图标 24px。

建议语义（名称仅为语义，不预设库）：

- `intent_ready`：文档/列表图标
- `clarify / needs_input`：对话气泡或问号圆
- `awaiting_confirm`：盾牌/确认圆
- `queued`：队列/时钟
- `running`：播放/进度环（静态或受 reduced-motion 约束）
- `completed`：勾选圆
- `failed`：警示圆
- `cancelled / denied`：停止/禁止
- `expired`：时钟加斜线
- `recovering`：刷新/同步

图标不应单独承担含义：同时呈现文本，且使用 `aria-label` 或隐藏文本说明状态。

### 5.2 可访问性要求

- 状态徽标使用文本+图标+（必要时）语义色，不能仅靠颜色区分；正文对比度目标 ≥4.5:1，图形/控件 ≥3:1。
- 所有确认、拒绝、取消、重试和查看详情控件键盘可达；焦点使用 `:focus-visible`，不可移除。
- 桌宠可聚焦并有明确 `aria-label`，如“后台任务：执行中，打开任务详情”；连接徽标另读为“语音链路：已连接”。
- 任务状态更新使用 `aria-live="polite"`；高风险确认/失败等需要立即行动的提示使用 `assertive`，但同一事件只播报一次。
- 触控目标：Android ≥48dp；Web ≥44px；相邻按钮至少 8px 间距。
- 动效使用 150–250ms 功能性过渡；`prefers-reduced-motion: reduce` 下移除旋转、脉冲和连续闪烁，保留文本/静态进度。
- 错误文案说明原因与下一步；不要只显示“失败”。超长摘要、任务名和路径截断时提供完整可读文本。
- 屏幕阅读器顺序：任务标题 → 事实状态 → 风险/进度 → 最近更新时间 → 主操作 → 次操作；内部 `task_id` 默认不朗读。

## 6. 断线、重启与迟到事件

| 场景 | 事实处理 | 桌宠/手机交互 | 语音策略 |
|---|---|---|---|
| WS 断线 | backend 任务继续；客户端缓存最后已知状态，不能改成失败 | 连接徽标显示“控制面重连中”；任务徽标保持最后事实并标“待同步” | 不播报连接错误为任务失败 |
| WS 恢复 | 通过快照/重放拉取最新任务，按 `version/event_id` 去重 | “已同步”短暂提示；终态若未读则进入队列 | 队列按已读语义只播报一次 |
| Android App 重启 | 读取 backend 持久化任务，不调用派发 | 任务列表显示最新状态；未确认任务仍需本次确认 | 不将旧任务当新任务播报 |
| backend 重启 | 未决/未知远端事实进入 `recovering`，恢复后再收敛 | 明确“正在恢复状态”，禁止显示“执行中”除非事实恢复为 running | “正在恢复，不会重复执行”最多一次 |
| Hermes 重启 | 保留 `remote_task_id`，通过 discovery/status/webhook 恢复 | 任务可显示“正在恢复”，不能创建副本 | 不重复播报受理 |
| webhook 迟到 | 以任务版本/终态规则拒绝回退；终态不回到 running | 若事件被丢弃，不覆盖当前 UI；可在详情时间线显示“已忽略迟到更新” | 不播报被拒绝的旧事件 |
| 重复事件 | 以 `event_id` 幂等 | 不闪烁、不重复 toast | 不重复语音 |
| 本地时间不准 | 以 backend `occurred_at/updated_at` 为准 | 相对时间不确定时显示绝对日期/时间 | 不朗读本地推断的时间 |

## 7. 一次性回报队列与已读语义

### 7.1 队列记录

后台结果持久化为按任务维度聚合的回报记录，而不是每个事件一条语音：

```text
report_id, task_id, status, summary, created_at, priority,
read_at, announced_session_id, version
```

推荐优先级：需要用户行动（`awaiting_confirm/needs_input`）> 失败/拒绝/过期 > 完成 > 取消。`running/queued` 不进入终态播报队列。

### 7.2 已读/已播报定义

- **已读（read）**：用户在手机任务详情或桌宠任务详情中看到该终态摘要，或明确点击“标为已读”。仅收到 WS 不算已读。
- **已播报（announced）**：TTS 成功开始播放该条摘要，并记录 `announced_session_id`；播放失败不标已播报。
- 同一 `task_id + status + version` 最多一次语音播报；更新到新版本生成新条目。
- 多条待播报在下一次语音会话合并为最多 2 条：先行动项，后最新终态；其余提示“还有 N 条结果，可在任务列表查看”。
- 用户正在说话或播放其他高优先级内容时，延后队列，不抢占当前对话。
- 清除/删除本地回报不会删除 backend 任务事实；清除动作只影响展示已读状态。

## 8. 避免多次点击“立即对话”

- “立即对话”只负责启动一次语音会话，不承担“提交任务/查看任务/确认任务”三种语义。
- 低风险委派从语音识别结果进入 brain 后，在同一受理流程自动进入 `queued/running`；桌宠和手机展示“已受理”，不出现第二个启动按钮。
- 高风险任务在 `awaiting_confirm` 只提供一个明确的“确认执行”主操作；确认可用语音口令完成，手机按钮是同一确认命令的备用入口，不是第二次启动。
- 任务完成后的“查看结果/再次执行”均创建或打开任务，不自动拉起语音会话。
- 语音会话结束后，下一次唤醒只播报队列；不得要求用户先点“立即对话”再点“查看任务”。
- 防重复点击：派发/确认提交后按钮进入 loading，使用 `task_id + version` 幂等；按钮只保留一个取消/关闭等下一步动作。

## 9. 任务列表与详情的最小信息架构

### 9.1 任务列表

每行只显示：

1. 任务摘要（脱敏、最多两行）。
2. 事实状态徽标（文本+SVG 图标）。
3. 风险/是否需要动作（如“需要确认”）。
4. 最近更新时间（backend 时间）。
5. 可选进度（仅 `queued/running`）。

筛选最多 4 个：全部、需要我处理、进行中、已结束。默认按“需要我处理 → 最近更新”排序。不要将 TRTC 连接和监听服务列入任务筛选。

### 9.2 任务详情

顺序固定：

1. 标题与事实状态。
2. 用户请求的脱敏摘要/目标范围。
3. 风险说明（`low/high/blocked`）与确认有效期（仅适用）。
4. 当前进度/下一步提示。
5. 结果摘要或错误分类。
6. 时间线（受理、确认、派发、进度、回报、重试、取消；按时间和版本去重）。
7. 主操作（确认/补充信息/重试/取消/查看结果）与次操作（关闭/标记已读）。

不默认展示远端任务 ID、token、完整日志和敏感路径；调试详情应在受控诊断入口中按权限显示。

## 10. Android `VoiceUiModel` / 旧 `VoiceUiState` 兼容迁移建议（不改 Kotlin）

### 10.1 迁移原则

- 保持 `VoiceUiModel` 作为语音 UI 唯一消费对象，继续承载 `experience/session/transcript/reply/rms/error`。
- 新增任务数据应作为**并行聚合字段/独立 StateFlow**（建议命名 `TaskUiModel` 或 `DelegationUiModel`），不要把 `task.status` 塞进 `ExperienceState`，也不要让任务状态污染 `VoiceSessionState`。
- `VoiceUiState` 旧字段继续兼容：`phase` 仅映射语音体验，`connection` 仅映射 TRTC，`service` 仅映射监听服务。旧界面迁移期间可由适配器读取四层聚合模型，但禁止业务层用布尔量拼装任务状态。
- 任务事件使用 `task_id/version/event_id` 去重；Android 进程重建时从 backend 快照恢复，不从旧本地语音状态猜任务事实。
- 语音播报队列作为独立的 `PendingReport` 流消费；`VoiceUiModel.reply` 仅表示当前语音回复，不能当作待播报持久队列。

### 10.2 建议的适配形状（概念，不是 Kotlin 改动）

```text
VoiceUiModel {
  experience: ExperienceState
  session: VoiceSessionState
  ...
}
TaskUiModel {
  active: TaskSummary?
  pendingActionCount: Int
  unreadReportCount: Int
  sync: "fresh|reconnecting|recovering"
}
```

旧 `VoiceUiState` 适配规则：`phase ← experience`、`connection ← trtc.connection`、`service ← listener.service`；不得以 `connection == CONNECTED` 设 `task.status = running`。

## 11. pet-ui WS 事件消费建议（backend 扩展为前置）

### 11.1 当前限制

`pet-ui/src/App.tsx` 当前监听 `session_updated / alert / pet_state`；`routes_ws.py` 当前只订阅广播会话、提醒和授权事件。虽然 `events.py` 已定义 `EVT_BRAIN_*` 常量，但这不等于 WS 已广播 brain 事件。实现前必须由 backend：

1. 定义并持久化统一 `brain_task` 事件包（见 §2.3）；
2. 在 `WsHub` 订阅任务事件并广播；
3. 提供 WS 连接后的任务快照/重放或等价 REST 查询，支持断线恢复；
4. 保证 HMAC 校验、任务版本、终态不可回退和重复事件幂等；
5. 明确哪些 `EVT_BRAIN_INTENT/TASK/INJECT` 映射到统一任务事实，避免客户端猜测。

### 11.2 前端消费边界

- `pet_state` 继续驱动语音球/宠物体验；`session_updated` 继续驱动监控会话；`brain_task` 只驱动任务徽标、列表、任务提醒和一次性回报队列。
- 收到 `brain_task` 时按 `task_id` 更新任务表，若 `version` 小于当前值则丢弃；未知任务先插入摘要，再等待快照补齐。
- WS 断开时不清空任务列表、不把任务设为失败；显示控制面连接状态独立徽标。
- “需要确认/补充信息/失败”才打开提醒或展开任务入口；普通 `queued/running` 不抢夺焦点。
- 不在客户端把 Hermes 监控窗口 `app_id=hermes` 当成任务状态源；委派事实必须来自 backend brain 事件。
- 后端未提供 `brain_task` 前，UI 只能保留设计占位/不显示伪任务状态，不得用 `session_updated` 或 `ConnectionBadge` 替代。

## 12. 设计 Token 与组件边界

本稿只引用现有设计 Token，不新增裸色值。颜色角色应使用既有 `--bg / --surface / --fg / --muted / --border / --accent / --success / --warn / --danger` 及 B-slot 语义别名；每屏可见 `--accent` 不超过两处。任务状态优先使用语义色+文字，深色模式用层级亮度而非阴影制造层次。

- 卡片/列表：`surface + border + radius-md`，不使用彩色左侧条纹，不叠加装饰性毛玻璃。
- 主操作：使用既有 primary/secondary/ghost 组件状态矩阵，覆盖 default/hover/focus/active/disabled/loading/error/success。
- 动效：状态确认 150ms，面板展开 200–300ms；`prefers-reduced-motion` 关闭脉冲/旋转。
- 图标：统一 SVG 描边库待架构决策；严禁 emoji、渐变文字、紫色到粉色渐变。
- 不新增营销 Hero、指标卡网格或大型控制面板；任务入口嵌入现有语音/桌宠表面。

## 13. 角色走查红旗

- **亚历克斯（高手）**：低风险任务若仍要求点“立即对话”或二次派发，会被视为阻塞；任务详情必须可直接看时间线和快捷操作。
- **山姆（无障碍用户）**：若徽标只有颜色/动画、焦点不可见、状态更新重复朗读，会无法判断任务事实；必须有文本、ARIA live 和键盘顺序。
- **凯西（移动用户）**：若完成结果只留在已结束语音会话、确认按钮在顶部且命中区过小，会错过关键动作；结果必须持久化，主操作固定且 ≥48dp。

## 14. OPEN：需要架构/产品确认

1. **图标库**：项目最终锁定哪一套 SVG 描边图标库（当前仅有 Lucide 使用痕迹，是否正式锁定待架构裁决）。
2. **统一任务事件契约**：`EVT_BRAIN_INTENT/TASK/INJECT` 是否继续对外，还是新增稳定的 `brain_task` 事件；快照/重放采用 WS 还是 REST。
3. **结果播报队列**：下一次会话最多播报 1 条还是 2 条行动/终态摘要；“已播报”以 TTS 开始还是播放完成为准。
4. **任务详情表面**：本期是否在 Android 手机展示完整详情，还是先提供摘要+操作、桌宠展示完整时间线。
5. **确认口令范围**：允许哪些明确肯定句；“嗯/好的/继续”在上下文唯一时是否仍要求复述动作。
6. **取消语义**：Hermes 不支持取消或进入不可逆阶段时，UI 是否显示“取消请求已提交”还是直接“无法撤销”。
7. **自动派发白名单**：低风险是否严格限于只读代码/日志/状态/分析；可逆写操作是否仍按 high。
8. **`recovering` 超时策略**：无法确认远端事实多久后转 `failed`/人工处理，是否提供最大等待时间。
9. **未读与已读跨端同步**：手机查看后桌宠是否同步清除；多设备是否需要用户级已读游标。
10. **任务列表容量与保留期**：终态任务保留多久、是否分页、是否允许用户删除本地展示记录。
11. **隐私展示级别**：任务摘要中允许展示到哪个路径/项目名粒度，调试详情需要何种权限。
12. **语言与播报**：中英文状态文案、TTS 语言切换和长文本结果摘要截断策略。

---

## 设计门禁自检

- [x] 四层状态事实分离，明确“已连接”不等于“执行中”。
- [x] 桌宠、手机、语音三种表面均有状态和动作策略。
- [x] 覆盖自动派发、确认、拒绝、过期、执行中、补充信息、完成、失败、取消、恢复。
- [x] 包含断线、重启、迟到/重复事件、一次性回报与已读语义。
- [x] Android 兼容迁移和 pet-ui WS 建议均不修改代码，明确 backend 广播前置。
- [x] 无 emoji 功能图标；图标库未锁定并列为 OPEN；颜色仅引用 Token；无紫粉渐变和营销 Hero。
- [x] 交互包含 focus-visible、键盘/触摸目标、ARIA live、reduced-motion 与状态矩阵要求。
