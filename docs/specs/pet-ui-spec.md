# 前端规格 — pet-ui 组件契约（A9）

> 版本：v1.0（M-1 修复基线）
> 日期：2026-08-03
> 状态：已确认 · 前端照做（现状组件已存在，本文件为接线/验收真源）
> 依据：docs/DESIGN.md（§2 token / §4 六态机 / §7 合规与 a11y）、docs/specs/design-alert-levels.md（四级打扰）、docs/openapi.yaml（WS 契约）、docs/specs/backend-capture-auth-spec.md（auth 事件）、pet-ui/src/state/petMachine.ts、pet-ui/src/state/wsClient.ts、pet-ui/src/styles/tokens.css
> 范围：V1 视觉组件接线 + a11y + token 合规；语音（VoiceOrb 播报态/V1.1）仅预留接入点。

---

## 1. 组件清单与职责

| 组件 | 文件 | 职责 | V1 状态 |
|---|---|---|---|
| Pet | `pet-ui/src/components/Pet.tsx` | 有机光球（SVG 渐变）：monitoring 80px/0.3 呼吸；alerting 140px/1.0 + 2Hz 脉冲；token 化颜色 | 已实现；M-1 接线四级打扰 + token 化（DESIGN §7.1） |
| VoiceOrb | `pet-ui/src/components/VoiceOrb.tsx` | 六态语音光球（phase 色 token 化） | 已实现；V1 仅静态渲染，播报态 V1.1 |
| MonitorPanel | `pet-ui/src/components/MonitorPanel.tsx` | 会话元数据：状态点/状态行（语义色）、帧数/耗时/时间、capture_mode 徽标 | 已实现；M-1 a11y A2 修复 |
| ReminderToast | `pet-ui/src/components/ReminderToast.tsx` | 提醒气泡（level≥3 显示，含建议 + 「我知道了」） | **M-1 拆分**：现内联 App.tsx，拆独立组件 + a11y A3-A5 |
| Settings | `pet-ui/src/components/Settings.tsx` | 主题切换（`data-theme`）、监控目标开关 | **M-1 拆分**：现内联 App.tsx，含主题持久化 |

## 2. 六态机事件契约（对照 petMachine.ts）

XState 状态：`idle / monitoring / listening / thinking / speaking / alerting`。事件来源=WS 映射（App.tsx）：

| 机器事件 | WS 来源 | 触发条件 | 行为 |
|---|---|---|---|
| WAKE | 点击宠物 / Settings | — | → listening（V1 点击仅转态，无真实语音，V1.1 接 O-003） |
| SPEECH_START | `pet_state=listening`（V1.1） | — | → listening |
| SPEECH_END | `pet_state=thinking`（V1.1） | — | → thinking |
| RESPONSE_START | `pet_state=speaking`（V1.1） | — | → speaking |
| RESPONSE_END | `pet_state=monitoring/idle`（V1.1） | — | → monitoring |
| **ALERT** | `alert` 事件 | **守卫 `level>=3`** | ≥3 → alerting（setAlert）；1/2 → **不转场**，仅 `context.alertLevel=N` 更新（design-alert-levels §4） |
| ALERT_DISMISS | toast 关闭 / 8s 自动 / ESC | — | alerting → monitoring，clearAlert |
| BARGE_IN | 后端重发 `pet_state=listening`（V1.1） | — | → listening |
| TIMEOUT | 静默 15s（前端定时器） | — | → monitoring |

**守卫实现（M-1 前端照做）**：`monitoring/listening/thinking/speaking` 态的 `ALERT` 事件必须加守卫区分高低级——低级走 action 留原态更新 `context.alertLevel`，`level>=3` 才转 `alerting`（现 App.tsx 二元化违反此条，见 design-alert-levels 关联缺陷）。

## 3. WS 事件映射表（对照 wsClient.ts + openapi.yaml + auth-spec）

| WS 事件（backend→UI） | 前端消费 | 状态 |
|---|---|---|
| `session_updated`（data: AgentSession） | MonitorPanel 更新；`capture_mode=pending-auth` → 显示授权引导入口；`status-only` → 显示降级徽标 | ✅ 已实现 |
| `alert`（data: {app_id, level, state, summary, suggestion}） | → ALERT 事件（§2 守卫）；level≥3 显示 ReminderToast | ✅ 已实现 |
| `auth_prompt`（data: {app_id, app_name, hint}） | 授权引导浮层（"请在系统弹窗允许捕获"）；可点"重新授权" → `POST /capture/authorize` | 🆕 V1 新增（auth-spec §4） |
| `auth_result`（data: {app_id, ok, mode, error}） | 收起引导；失败提示"已降级为状态监控" | 🆕 V1 新增 |
| `pet_state`（V1.1） | SPEECH_START/END、RESPONSE_START/END、BARGE_IN | ⏳ V1.1（openapi x-phase） |
| `voice_transcript`（V1.1） | 转录文本显示 | ⏳ V1.1 |
| `pong` / `ack` | 心跳/指令应答 | ✅ 已实现 |

**心跳/重连（wsClient.ts 已实现，契约保持）**：UI 每 15s `ping`；断线指数退避 1s→30s 上限；`onmessage` 非 JSON 忽略。

## 4. Token 引用要求（对照 tokens.css + DESIGN §7.1）

1. **禁字面量**：除 `tokens.css` 外，任何 `.tsx/.ts/.css` 不得出现 `#hex`/`rgb(a)`——一律 `var(--token)`；全项目扫描命令（DESIGN §7.1）：`grep -rnE "#[0-9a-fA-F]{3,8}|rgba?\(" src --include=*.tsx --include=*.ts`，仅 tokens.css 允许命中。
2. **SVG 渐变**：`stopColor`/`fill` 不能写属性字面量，必须 `style={{ stopColor: "var(--token)" }}`。
3. **语义色映射**（现状违规清零，DESIGN §7.1 表）：
   - Pet `TONE_COLOR`：neutral→`var(--info)`、success→`var(--success)`、warn→`var(--warn)`、danger→`var(--danger)`；高光 stop/ellipse→`var(--pet-core-hi)`。
   - VoiceOrb `PHASE_COLOR`：idle/monitoring/listening/speaking→`var(--info)`、thinking→`var(--thinking)`、alerting→`var(--danger)`。
   - App toast 阴影→`var(--toast-shadow)`（替代 `rgba` 字面量）。
4. **四级打扰视觉参数**（design-alert-levels §2 总表，前端照做）：
   - L1：80px/0.3 监控态，仅状态点变色（warn/danger）；不 toast、不转 alerting。
   - L2：单次呼吸 `scale 1.0→1.03→1.0`、0.6s、ease-out、iteration=1；`animationend` 清 class 支持重触发。
   - L3/L4：140px/1.0/2Hz 脉冲（`pet-alert` 0.5s 周期 scale 1.0→1.06）+ ReminderToast；**面板不自动展开**；8s 自动回落或 ALERT_DISMISS。
   - `prefers-reduced-motion`：L2 跳过动效、L3 静态放大（scale 1.03）+ 语义色，颜色通道保留。

## 5. a11y 验收（对照 DESIGN §7.2，M-1 全项通过）

| # | 项 | 验收标准 | 组件 |
|---|---|---|---|
| A1 | pet-anchor 可访问 | `role="button"` + `tabIndex={0}` + 动态 `aria-label`（开/关面板）+ `aria-expanded`；Enter/Space 触发；`:focus-visible` 显 `--focus-ring`；点击区 ≥44×44px | Pet/App |
| A2 | 元数据对比度 | 帧数/耗时/时间行：`font-size 11px→12px`、`var(--muted)→var(--fg-2)`（深 7.18:1 / 浅 7.07:1） | MonitorPanel |
| A3 | toast 语义 | `role="alert"` + `aria-live="assertive"`；含 app_id/state/summary/suggestion | ReminderToast |
| A4 | toast 关闭钮 | Lucide `X`（20px）+ `aria-label="关闭提醒"` + 键盘可达 | ReminderToast |
| A5 | 焦点管理 | 打开焦点移入关闭钮；关闭/回落焦点归还 pet-anchor；ESC 关闭 | App/ReminderToast |
| A6 | 状态不靠颜色 | 状态点同时保留图标差异（CheckCircle2/AlertTriangle/XCircle） | MonitorPanel |
| A7 | reduced-motion | global.css 全局降级 + design-alert-levels §5 四级降级 | 全局 |
| A8 | 键盘导航 | 全部交互元素 Tab 可达 + 可见焦点环；无仅 hover 交互 | 全局 |

## 6. 归属判断

| 项 | 归属 |
|---|---|
| 四级打扰接线（ALERT 守卫 + L1/L2 不转场 + L3/L4 渲染）、ReminderToast/Settings 拆分、token 化、a11y A1-A8 | **V1 立即实现（M-1）** |
| 语音六态机接线（SPEECH_*/RESPONSE_*）、VoiceOrb 播报态、BARGE_IN | **V1.1**（openapi x-phase） |
| 授权引导浮层（auth_prompt/auth_result） | **V1**（依赖 backend auth-spec §4） |

## 7. 验收清单（照做）

- [ ] ALERT level 1/2 不触发 Pet alerting（保持 80px/0.3 监控态）；level≥3 才转场
- [ ] L2 单次呼吸 0.6s/iteration=1/可重触发；L3/L4 140px/100%/2Hz + toast；面板不自动展开
- [ ] 恢复事件（state=progress）→ 任意态回 monitoring 并 `clearAlert`
- [ ] token 扫描命令仅 tokens.css 命中；SVG 渐变走 style var()
- [ ] a11y A1-A8 逐项自测通过
- [ ] `session_updated.capture_mode` 驱动：pending-auth 显示授权引导；status-only 显示降级徽标

## 8. E2E 验证

1. `POST /api/v1/control` trigger_alert_test → 桌宠 L3/L4 渲染 + toast 出现；8s 自动回落；`prefers-reduced-motion` 下静态放大。
2. level 1/2（构造低级别检测）→ 宠物保持监控态、仅状态点变色、无 toast。
3. 后端重启后窗口 pending-auth → UI 授权引导 → 点授权走通 auth_prompt/auth_result（联动 backend auth-spec §10）。
