# 设计规格 — 四级渐进打扰前端方案（P0 契约）

> 版本：v1.0（M-1 修复基线）
> 日期：2026-08-03
> 状态：已确认 · 供前端 M-1 照做
> 依据：PRD D-3（四级渐进打扰）、docs/DESIGN.md §3/§4、O-008
> 关联缺陷：审计 §7.1「四级渐进打扰二元化」——现 App.tsx 将 level 1/2 也渲染为完整提醒态（140px/100%/脉冲），违反「1-2 级不动声色」。

---

## 1. 设计原则（先行约束）

1. **不动声色优先**：level 1/2 不得改变宠物形态与透明度，只允许最轻量视觉提示。
2. **动效分级即打扰分级**：动效参数是打扰强度的直接载体，禁止越级。
3. **唯一弹跳态**：脉冲（2Hz）只属于 level ≥3；level 2 的单次呼吸为一次性微动，非循环。
4. **语义双通道**：任何级别保留语义色（warn/danger）通道，动效可被 `prefers-reduced-motion` 降级，颜色不降级。

---

## 2. 四级视觉规格总表

| 级别 | 宠物尺寸 | 宠物透明度 | 宠物形态 | 动效 | 动效参数 | 面板 | 提醒气泡 | 状态机 |
|---|---|---|---|---|---|---|---|---|
| L1 状态点变色 | 80px | 0.3 | monitoring（不变） | 无升级（维持 6s 呼吸循环） | — | 状态点/行色切换为 warn/danger | 不显示 | 不切 alerting，仅 `context.alertLevel=1` |
| L2 桌宠微动 | 80px | 0.3 | monitoring（不变） | **单次**呼吸加速 | scale 1.0→1.03→1.0，0.6s，ease-out，iteration=1 | 同 L1 | 不显示 | 不切 alerting，仅 `context.alertLevel=2` |
| L3 浮起放大+脉冲 | 140px | 1.0 | alerting（放大浮起） | 2Hz 脉冲（现 `pet-alert`） | 0.5s 周期，scale 1.0→1.06，infinite | 同 L1 | 显示 ReminderToast（含建议） | ALERT 事件 → alerting |
| L4 语音+推送 | 140px | 1.0 | alerting（同 L3） | 同 L3（前端无新增动效） | 同 L3 | 同 L1 | toast 附建议 +「我知道了」 | alerting（同 L3） |

**升级路径**：L1 → L2 → L3 → L4 逐级递进；任一级状态恢复（D-4，state 变 progress）→ 立即回 Monitoring 并清 `alertLevel=0`。

---

## 3. 各级实现要点（前端照做）

### L1 — 状态点变色

- Pet 渲染：`mode="monitoring"`、`sizePx=80`、`opacity=0.3`、`alertPulse=false`（**维持监控态全部参数**）。
- MonitorPanel：状态点与状态行文字颜色由 `STATE_META` 语义色驱动（已实现，`var(--warn)` / `var(--danger)`）。
- 禁止：宠物任何尺寸/透明度/动效变化；禁止 toast；禁止自动展开面板。
- 回落：无自动回落，保持至升级或状态恢复。

### L2 — 桌宠微动（单次呼吸加速）

- Pet 渲染：仍为监控态（80px/0.3），**不加边框、不放大、不加脉冲环**。
- 动效：`scale 1.0 → 1.03 → 1.0`，时长 **0.6s**，缓动 `cubic-bezier(0.2, 0, 0, 1)`，`animation-iteration-count: 1`（单次，非循环）。
- 重触发机制：`alertLevel` 从 0/1 升到 2 时向 pet 追加一次性 class（如 `pet-nudge`）；用 `animationend` 事件移除 class 以便下次可重触发（避免同级重复 alert 不响应）。
- 面板：状态点已变色（同 L1），无其他变化。
- 禁止：尺寸/透明度变化；循环动画；toast。

### L3 — 浮起放大 + 脉冲

- Pet 渲染：`mode="alerting"`、`sizePx=140`、`opacity=1.0`、`alertPulse=true`（复用现 `pet-alert`，0.5s 周期 scale 1.0→1.06）。
- 语义色：由 `tone`（stuck→warn / off_track→danger）驱动，沿用现有 TONE_COLOR→Token 映射（见 DESIGN.md §7）。
- ReminderToast：显示，内容 = `app_id + state 中文 + summary + suggestion`（已有字段）。
- **面板默认不自动展开**（与现 App.tsx 差异：移除 `level>=3 → setShowPanel(true)`）——保持视线不被打断，toast 为详情入口，用户点宠物/toast 查看面板。
- 回落：8s 自动回落或用户关闭（ALERT_DISMISS）→ Monitoring。

### L4 — 语音 + 推送

- 视觉：与 L3 完全一致（前端无新增动效）。
- 推送：由后端 `reminder_service` 触发本机 webhook（前端不实现，属 P-* 后端链路）。
- 前端增强：toast 附建议（`suggestion` 字段，已有）+ 操作按钮「我知道了」（关闭并回落）。
- 语音播报：属 V1.1 O-005，前端仅预留 VoiceOrb 播报态接入点，V1 不实现。

---

## 4. 状态机映射（petMachine 接线契约）

| 事件 | level | 行为 |
|---|---|---|
| ALERT | 1 / 2 | **不转场**：留在当前态，`assign({ alertLevel: N })` 更新 context；面板色由 level/state 驱动 |
| ALERT | ≥3 | 守卫 `level>=3` 通过 → 转 `alerting`（setAlert） |
| ALERT（已在 alerting） | ≥3 | 留在 alerting，更新 level + suggestion（不重置转场） |
| ALERT_DISMISS / 8s 超时 | — | alerting → monitoring，`clearAlert` |
| 状态恢复（state=progress） | — | 任意态 → monitoring，`clearAlert`（D-4） |

**实现提示（前端）**：`monitoring` 态的 `ALERT` 事件需加守卫区分高低级——低级走 action 留在 monitoring，高级才转场；`guard` 语义见上表。`setAlert` 已含 level/app_id 写入，补充写入 `suggestion`。

---

## 5. 无障碍与降级

- `prefers-reduced-motion: reduce`：
  - L2 单次呼吸 → 跳过动效（仅颜色，无透明度变化）；
  - L3 2Hz 脉冲 → 静态放大（scale 1.03 常驻）+ 语义色，不加循环动画；
  - 语义色通道保留（视觉不靠动效传达）。
- 提醒双通道：L3/L4 必须视觉（宠物+toast）双通道；L4 推送为后端通道。
- 打断响应 <100ms：新 alert 覆盖旧 alert 时立即更新，不等待旧动画结束。

---

## 6. 前端验收清单

- [ ] WS `alert` level 1/2 不触发 Pet alerting 渲染（保持 80px/0.3 监控态）
- [ ] L2 单次呼吸 0.6s、iteration=1、可重触发（animationend 清 class）
- [ ] L3/L4 渲染 140px/100%/2Hz 脉冲 + ReminderToast；面板不自动展开
- [ ] level≥3 才转 `alerting`；恢复事件清 `alertLevel=0`
- [ ] reduced-motion 降级符合 §5
- [ ] 无 emoji 作图标；无 hex/rgba 字面量（除 tokens.css，见 DESIGN.md §7 P0-2）
