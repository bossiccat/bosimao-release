# 后端规格 — WGC 授权流程（P0 契约）

> 版本：v1.0（M-1 修复基线）
> 日期：2026-08-03
> 状态：已确认 · 供后端 M-1 照做（依赖项见 §8）
> 依据：PRD 捕获链路、docs/poc/POC-002-wgc-capture.md、docs/openapi.yaml（v1.1）、backend/app/capture/session_manager.py、backend/app/core/orchestrator.py
> 关联缺陷：session_manager 已有 `authorized`/`mode` 字段与 `pending-auth` 态，但缺少授权触发接口、持久化与失败降级路径。

---

## 1. 问题与目标

WGC（Windows 图形捕获）首次捕获被监控窗口时必须获得系统授权（系统窗口选择器/授权弹窗）。现状缺口：

1. `locate_all()` 已能产出 `pending-auth` 态，但**没有**把窗口推到授权流程的入口；
2. 授权结果无持久化——后端重启后授权丢失，需重复走系统弹窗；
3. 授权被拒/超时**没有**降级路径，该窗口会永久停留在 `pending-auth` 且无法监控；
4. 前端无授权引导事件，用户不知道要"去系统弹窗点允许"。

本契约补齐：授权触发接口 + 状态机 + 持久化 + WS 引导事件 + 拒绝降级，使 WGC 链路在 M-1 内可用。

---

## 2. 状态流转（单一真源）

```
                          ┌────────────────────────────────────────────┐
                          │                                            │
 locate_all 找到窗口        v    POST /capture/authorize                 │
 ──────────────► pending-auth ─────────► authorizing ──── 首帧到达 ────► authorized ──► start_wgc ──► wgc
                      │                      │  ▲                            │
                      │                      │  │                            │
                      │                      │  └── 超时(60s)/系统拒绝/捕获异常 ─┘
                      │                      └────────► denied ──────────────► status-only（降级监控）
                      │
 窗口未找到/进程退出 ──► none（沿用现状）
```

**字段语义**（`CaptureSession.mode`，与 openapi `capture_mode` 对齐）：

| mode | 含义 | 现有 |
|---|---|---|
| `none` | 窗口未找到 | ✅ session_manager 已有 |
| `pending-auth` | 窗口已找到，未授权，等待授权 | ✅ 已有 |
| `authorizing` | 授权进行中（已触发系统选择器，等待结果） | 🆕 新增 |
| `authorized` | 授权成功（会话内标志位，同 `session.authorized=True`） | ✅ authorized 已有 |
| `denied` | 授权被拒/超时（本窗口本次运行不再自动重试） | 🆕 新增 |
| `status-only` | 拒绝降级：仅窗口存在性/进程状态监控，不截屏分析 | 🆕 新增 |
| `wgc` | WGC 捕获运行中 | ✅ 已有 |
| `dxgi` / `lost` | DXGI 兜底 / 窗口丢失 | ✅ 已有 |

> **openapi 修订点（实施时同步，文档阶段不改 openapi.yaml）**：`AgentSession.capture_mode` 枚举需追加 `authorizing` / `denied` / `status-only` 三个值；当前枚举 `[wgc, dxgi, none, pending-auth, lost]` 不含它们。本 spec 为契约真源。

---

## 3. HTTP 接口（对照 openapi.yaml 风格）

### 3.1 POST /api/v1/capture/authorize

```yaml
/capture/authorize:
  post:
    summary: 触发指定窗口的 WGC 授权（系统窗口选择器/授权引导）
    operationId: authorizeCapture
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required: [app_id]
            properties:
              app_id: { type: string, example: codex }
              retry: { type: boolean, default: false, description: true=手动重试已拒绝窗口（重置 denied） }
    responses:
      "202":
        description: 授权流程已触发，等待系统选择器结果（WS auth_prompt 已下发）
        content:
          application/json:
            schema:
              type: object
              properties:
                accepted: { type: boolean, example: true }
                app_id: { type: string, example: codex }
                mode: { type: string, enum: [authorizing, wgc], example: authorizing }
      "400":
        description: 非法请求（app_id 未知 / 该窗口已 authorized）
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/ErrorBody"
      "409":
        description: 授权进行中（重复触发，幂等返回当前状态）
      "503":
        description: 后端未初始化 / 系统选择器不可用
```

- 幂等性：`authorizing` 期间重复 POST 返回 `409` + 当前状态，不重复弹系统选择器。
- `retry=true`：仅对 `denied`/`status-only` 窗口有效，重置为 `authorizing` 重新触发。

### 3.2 错误体（对齐 openapi 全局错误码）

```yaml
ErrorBody:
  type: object
  properties:
    code: { type: integer, example: 40402 }
    message: { type: string, example: "未知 app_id: codex" }
    data: { type: object, nullable: true }
```

错误码约定（延续 RESTful 规范）：`40401` app_id 未知 / `40901` 授权进行中 / `50301` 服务未就绪。

---

## 4. WS 事件（backend → UI，新增两个）

沿用 `/ws/pet` 现有信封 `{"type":"event","event":...,"data":...}`，在 `events.py` 增加常量 `EVT_AUTH_PROMPT`/`EVT_AUTH_RESULT`，由 `WsHub` 订阅广播（对齐现有 `_broadcast_session`/`_broadcast_alert` 模式）。

```yaml
# 方向：backend → UI
{type: "event", event: "auth_prompt", data: {app_id, app_name, hint: "请在系统弹窗中允许捕获窗口 <title>"}}
  # 时机：进入 authorizing 时下发；前端据此显示授权引导浮层（角色说明见 pet-ui-spec §4）

{type: "event", event: "auth_result", data: {app_id, ok: true|false, mode: "authorized"|"denied"|"status-only", error: string|null}}
  # 时机：授权流程结束（成功/拒绝/超时）时下发；前端据此收起引导、按结果提示或降级提示
```

同时复用现有 `session_updated`：授权过程中 `capture_mode` 变化（pending-auth→authorizing→wgc/status-only）会随监控循环广播，前端状态点据此渲染。

> **归属判断**：WS 两个事件 **V1 立即实现**（授权可用性是 V1 WGC 捕获的前置条件）。

---

## 5. 持久化（backend/data/authorized_windows.json）

- 位置：`backend/data/authorized_windows.json`（首次写入时自动建目录，目录随代码仓库忽略，`backend/data/.gitignore` 加入 `*.json`）。
- 结构（**不含敏感信息**：无 token、无 webhook、无截图、无命令行）：

```json
{
  "version": 1,
  "windows": [
    { "app_id": "codex", "window_title": "Codex", "authorized": true, "authorized_at": 1780000000 }
  ]
}
```

- 字段约束：`window_title` 仅用于后端本地 WGC 捕获匹配（`WgcCapturer(window_title=...)`），禁止写入日志、禁止经 WS/HTTP 上报。
- 读写：
  - 读：`SessionManager.__init__` 加载；`locate_all()` 命中已授权窗口时直接置 `authorized=True`（跳过授权流程）。
  - 写：授权成功时 `mark_authorized(app_id)` 后同步落盘（原子写：写临时文件 + `os.replace`）。
  - 删除：用户移除监控目标或显式撤销时删除对应项（V1 仅提供配置侧移除，不做独立 API）。
- 失败容忍：文件缺失/损坏 → 视为全部未授权，重新走授权流程，不阻断启动。

---

## 6. session_manager.py 衔接（改名/新增清单）

| 现文件行为 | 契约要求 |
|---|---|
| `CaptureSession.mode` 默认 `"none"` | 保持；新增 `authorizing/denied/status-only` 取值 |
| `locate_all()`：`mode = "wgc" if authorized else "pending-auth"` | 改为读持久化：`mode = "wgc" if session.authorized else "pending-auth"`；若持久化命中 → `authorized=True` |
| `start_wgc(app_id)`：`not authorized → return False` | 保持（未授权必须先走授权）；新增前置：`mode in ("denied","status-only")` 拒绝启动并返回 False + `last_error` |
| `mark_authorized(app_id)` | 保留；新增 `mark_denied(app_id, reason)` 与 `set_authorizing(app_id)` 状态写入 |
| — | 新增 `authorize(app_id) -> bool`（orchestrator 调用）：置 `authorizing` → 触发 WGC 试捕获/系统选择器 → 首帧/超时/异常判定 → 回写 `authorized`/`denied` → 落盘 → 发 WS 事件 |
| — | `to_dict()` 补 `mode` 全部新值（现仅返回 5 个字段，不影响 openapi AgentSession 消费） |

**orchestrator 接线**：`authorize()` 由 `routes_control`（或新 `routes_capture.py`）注入调用；`orchestrator.start()` 的 `locate_all()` 后，对 `enabled` 且已授权窗口 `start_wgc`（现状逻辑），对未授权窗口保持 `pending-auth` 并**不下发自动弹窗**（避免启动即打扰），由前端收到 `capture_mode=pending-auth` 后引导用户点击"授权"。

---

## 7. 超时 / 重试 / 失败降级

| 场景 | 策略 |
|---|---|
| 授权超时（进入 `authorizing` 后 **60s** 无首帧、无异常） | 判定 `denied`，`error="授权超时"`，降级 `status-only`；发 `auth_result{ok:false}` |
| 系统选择器被取消/拒绝 | 捕获初始化抛异常或 0 帧 → 判定 `denied`，`error="用户拒绝/选择器取消"`，降级 `status-only` |
| 窗口在授权期间消失 | 判定 `denied`，`error="窗口丢失"`，mode 置 `none`（重新 locate 后可再触发） |
| 用户重试 | 前端"重新授权"按钮 → `POST /capture/authorize {app_id, retry:true}` → 重置 `denied` → 重新 `authorizing`（单窗口最多连续 3 次，第 4 次起需 60s 冷却；防刷弹窗） |
| 后端重启 | 持久化命中 → 直接 `authorized` + 自动 `start_wgc`；未命中 → `pending-auth` 等用户引导 |

**status-only 降级语义**：该窗口不再截屏/视觉分析（orchestrator `_tick_one` 跳过 `snapshot()`），仅上报 `window_found` 与进程存活；`state` 维持 `unknown`，`last_summary="未授权，仅状态监控"`；用户授权成功后自动切回 `wgc` 恢复正常监控（`locate_all` + `start_wgc` 触发）。

---

## 8. 依赖与归属判断

| 项 | 归属 | 依赖 |
|---|---|---|
| 授权状态机 + `POST /capture/authorize` + 持久化 + WS 两事件 + `status-only` 降级 | **V1 立即实现** | 无（接口/状态机先行） |
| 授权触发实现（系统选择器/弹窗的具体调起方式） | **V1 立即实现** | **依赖 PoC B2 实测**：`windows-capture` 首捕是自动弹窗还是需调 `GraphicsCapturePicker`，B2 通过后回填 §6 `authorize()` 实现细节（本 spec 只定接口与状态机，触发方式以 B2 结论为准） |
| `capture_mode` 新枚举值（authorizing/denied/status-only）同步 openapi | **V1 落地时** | 实施阶段改 openapi（本 spec 已登记，文档阶段不改） |
| 手动窗口选择器 GUI（用户从列表选窗口） | **V1.1** | O-004 备选 B；仅当 PoC B2 标题匹配失败才启用 |

---

## 9. 验收清单（照做）

- [ ] `POST /api/v1/capture/authorize` 对未知 app_id 返 40401，对 authorizing 中重复触发返 40901
- [ ] 授权成功：`session.authorized=True`、`mode="wgc"`、`authorized_windows.json` 落盘、WS `auth_result{ok:true}`、`start_wgc` 生效
- [ ] 授权拒绝/超时：`mode="status-only"`、WS `auth_result{ok:false}`、orchestrator 不再对该窗口截屏分析
- [ ] 重启后已授权窗口自动 `authorized` + `start_wgc`（无需重新授权）
- [ ] `authorized_windows.json` 不含 token/webhook/截图/命令行；写入原子、损坏可容忍
- [ ] WS 信封符合 `{type:"event", event:"auth_prompt"/"auth_result", data:{...}}`；前端引导浮层能随 `auth_result` 收起

## 10. 端到端验证（E2E）

1. 启动后端 → `/api/v1/status` 中 codex `capture_mode=pending-auth`；
2. UI 收到 `session_updated`（pending-auth）→ 显示授权引导 → 用户点"授权" → `POST /capture/authorize {app_id:"codex"}` → WS 收 `auth_prompt`；
3. 系统弹窗点"允许" → 数秒内 WS 收 `auth_result{ok:true}` → `/api/v1/status` codex `capture_mode=wgc`，`/api/v1/status/sessions/codex` `last_summary` 出现视觉判定文本；
4. 重启后端 → codex 直接 `wgc`（无弹窗）；
5. 授权窗口点"拒绝" → `capture_mode=status-only`，`/api/v1/control/test-push` 仍可用（推送链路不依赖截屏）。
