# 设计契约 — 贾克斯模式：AI 智能体监控中枢

> 版本：v1.1（M-1 修复基线 · 设计契约修订）
> 日期：2026-08-03（v1.1 修订同日）
> 状态：已确认（M-1 修复基线执行中）
> 依据：docs/design-research-phase1.md（Phase 1 设计调研）+ token-standard.md + docs/specs/design-alert-levels.md（M-1 新增四级打扰规格）

---

## 1. 设计语言

**「有机科技 · 冷静陪伴」(Organic Tech, Calm Companion)**

- 风格关键词：发光的活体传感器 / 显微镜下的荧光细胞 / 深空里的一颗小行星
- 深色开发者环境（非纯黑，带冷蓝调）
- 宠物 = 有机光体（柔和边缘 + 核心高光，像"有生命的传感器"）
- UI 面板 = 克制深色 + 1px 边框 + 低阴影（Product 寄存器）
- 动效 = 克制、有物理感（快攻击慢衰减）；状态靠材质/运动表达，不靠纯颜色
- **反 AI 模板**：不用紫粉渐变、面板不用装饰性毛玻璃、不用发光边框堆砌

**寄存器**：Product 为主（监控面板/语音对话/设置走 Linear/Vercel 质感）+ 宠物层品牌情感（光球允许有机非对称）。

**三轴刻度**：`DESIGN_VARIANCE=5` / `MOTION_INTENSITY=6`（宠物是核心动效载体，提醒克制）/ `VISUAL_DENSITY=5`。

## 2. Design Token（全表）

### 2.1 深色主题（默认）

**A1-identity**

| Token | 值 | 角色 |
|---|---|---|
| `--bg` | `#0B0E14` | 深空蓝黑 |
| `--surface` | `#12161F` | 面板/弹窗 |
| `--surface-2` | `#1A202C` | 悬浮层/下拉 |
| `--fg` | `#E6EAF2` | 主文本 |
| `--fg-2` | `#9AA4B2` | 次级文本 |
| `--muted` | `#6B7686` | 弱化文本 |
| `--accent` | `#38BDF8` | 品牌/宠物核心光色 |
| `--border` | `rgba(255,255,255,0.08)` | 默认边框 |
| `--border-soft` | `rgba(255,255,255,0.05)` | 内部行分隔 |

**A2-semantic（监控状态语义色 — 核心）**

| Token | 值 | 语义 |
|---|---|---|
| `--success` | `#34D399` | 有进展 |
| `--warn` | `#F59E0B` | 卡住/停滞 |
| `--danger` | `#F87171` | 跑偏/异常 |
| `--info` | `#38BDF8` | 信息/对话 |
| `--focus-ring` | `rgba(56,189,248,0.4)` | 焦点环 |
| `--thinking` | `#818CF8` | 思考态（Indigo 纯色，P0-2 允许） |

**宠物本体光色**：`#2563EB → #38BDF8`（深蓝→天蓝，同色系渐变，非紫粉）

**M-1 补充 Token（新增，tokens.css 已实现）**

| Token | 值 | 角色 |
|---|---|---|
| `--pet-core-hi` | `#FFFFFF` | 宠物核心高光（白，SVG stopColor 引用） |
| `--elev-raised` | `0 8px 30px rgba(0,0,0,0.4)` | 提醒气泡/浮层阴影（替代硬编码 box-shadow） |
| `--toast-shadow` | `var(--elev-raised)` | ReminderToast 阴影别名 |
| `--accent-strong` | `#0284C7`（浅色）/ `#38BDF8`（深色） | 需 4.5:1 对比度的 accent 文本/交互用途 |

> 注：宠物渐变 stop 不再用 `stopColor` 属性写死色值，统一 `style={{ stopColor: "var(--pet-core-*) / var(--info)" }}`（SVG 属性不支持 var()，须走 style 内联）。

### 2.2 浅色主题（备用，设置可切）— 契约已定稿（M-1 前端落地）

> 实现状态：**设计契约定稿**（下表 + 下方 CSS 片段为唯一真源）；`tokens.css` 落地方案已产出，**由前端 M-1 照抄实现**（审计 #：原 §2.2 有定义但 tokens.css 未实现）。
> 切换方式：由 `Settings.tsx` 组件切换 `<html data-theme="light|dark">` 属性（默认深色）；设置项持久化到 localStorage，启动时读取。
> 对比度承诺：正文/次级文本与深色主题等价（fg 15.96:1 / fg-2 7.53:1 / muted 4.76:1，白底实测）。

```css
/* tokens.css — 浅色主题（前端 M-1 照抄追加） */
:root[data-theme="light"] {
  /* A1-identity */
  --bg: #F6F8FB; --surface: #FFFFFF; --surface-2: #EFF2F7;
  --fg: #1A2230; --fg-2: #4A5568; --muted: #64748B;
  --accent: #0EA5E9; --accent-strong: #0284C7;
  --border: #E2E8F0; --border-soft: #EEF2F7;
  /* A2-semantic */
  --success: #059669; --warn: #D97706; --danger: #DC2626;
  --info: #0284C7; --focus-ring: rgba(14,165,233,0.35);
  --thinking: #6366F1;
  /* 宠物与阴影 */
  --pet-core-hi: #FFFFFF;
  --pet-core-start: #2563EB; --pet-core-end: #0EA5E9;
  --elev-raised: 0 4px 16px rgba(15,23,42,0.12);
}
```

**A1-identity**

| Token | 值 | 角色 | 对比度(白底) |
|---|---|---|---|
| `--bg` | `#F6F8FB` | 页面背景 | — |
| `--surface` | `#FFFFFF` | 面板/弹窗 | — |
| `--surface-2` | `#EFF2F7` | 悬浮层/下拉 | — |
| `--fg` | `#1A2230` | 主文本 | 15.96:1 |
| `--fg-2` | `#4A5568` | 次级文本 | 7.53:1 |
| `--muted` | `#64748B` | 弱化文本 | 4.76:1 |
| `--accent` | `#0EA5E9` | 品牌/宠物光色（非文本用途） | 2.77:1（装饰） |
| `--accent-strong` | `#0284C7` | 需 ≥4.5:1 的 accent 文本/交互 | 4.10:1 |
| `--border` | `#E2E8F0` | 默认边框 | — |
| `--border-soft` | `#EEF2F7` | 内部行分隔 | — |

**A2-semantic**

| Token | 值 | 语义 | 对比度(白底) |
|---|---|---|---|
| `--success` | `#059669` | 有进展 | 3.77:1（非文本点） |
| `--warn` | `#D97706` | 卡住 | 3.19:1（非文本点） |
| `--danger` | `#DC2626` | 跑偏/异常 | 4.83:1 |
| `--info` | `#0284C7` | 信息/对话 | 4.10:1 |
| `--focus-ring` | `rgba(14,165,233,0.35)` | 焦点环 | — |
| `--thinking` | `#6366F1` | 思考态（Indigo 纯色，P0-2 允许） | 4.47:1 |
| `--pet-core-hi` | `#FFFFFF` | 宠物高光 | — |
| `--elev-raised` | `0 4px 16px rgba(15,23,42,0.12)` | 浮层阴影（浅色降档） | — |

> 浅色宠物渐变保持品牌色 `#2563EB → #0EA5E9`（同系，非紫粉）；`--pet-core-start/end` 随主题覆盖。
> 语义色 success/warn 在浅色下仅用于状态点等非文本图形（WCAG 1.4.11 非文本 ≥3:1 已满足 3.77/3.19）；文本用途须走 `--fg-2`/`--danger`/`--accent-strong`。

### 2.3 强调色使用规则

- 每屏 ≤2 处 accent；监控面板主色只出现在"宠物本体 + 当前状态指示"
- 语义色仅用于状态点/通知徽标，不扩散到大面积

## 3. 宠物形象规范（有机光球）

### 双态设计（贯穿）

| 形态 | 尺寸 | 透明度 | 表现 |
|---|---|---|---|
| 监控态 | 64-96px | 20-40% | 贴边/角落，低幅呼吸，不遮挡视线 |
| 提醒态 | 120-160px | 100% | 浮起放大 + 语义色 + 脉冲 2Hz + 可配语音/推送 |

### 视觉构成

- 一团流动的光：核心高光（似眼睛，可做睁眼/闭眼/关注微表情）+ 柔和能量外壳 + 边缘羽化
- 纯代码绘制（SVG 径向渐变 + 少量粒子），零贴图素材

### 皮肤扩展（方向 2 预留）

机械核心/浮游哨兵（六边形核心 + 雷达眼）作为"硬核开发者皮肤"备选，走皮肤目录（assets/lottie + config/pet.json 切换）。

> **M-1 处置标注**：`pet-ui/src/assets/lottie/` 为**皮肤扩展预留目录**（空目录，保留 `.gitkeep`），**非 V1 交付物**——V1 宠物为纯代码 SVG 绘制（零贴图素材），空目录属预期，非缺陷。皮肤加载器排 V1.1+（O-008 关联）。

### 命名建议

宠物命名「星核 Spark」，增强情感连接。文案避免空洞。

## 4. 语音六态状态机

```
Monitoring（常驻 8s 低频扫描）──唤醒──▶ Listening ──输入结束──▶ Thinking ──响应──▶ Speaking
      ▲                                                                        │
      └────────────────── 打断(barge-in) 任意态 ──▶ Listening ◀──────────────────┘
      ▲                            │
      └────── 空闲回落(15s) ◀───────┘
Monitoring ──事件──▶ Alerting（提醒）──确认/关闭──▶ Monitoring
```

| 状态 | 视觉表现 | 颜色 | 动效参数 |
|---|---|---|---|
| Idle 待机 | 缩小贴边，核心缓慢呼吸 | accent 20% | scale 1.0→1.02，6s 周期 |
| Monitoring | 核心眼缓慢扫描，头顶微型状态点（三 App） | accent + 语义点 | 8s 低频 |
| Listening | 微放大，外层声呐脉冲环（幅度跟随音量） | accent 渐亮 | 脉冲环 0.8s |
| Thinking | 内部流动加速（6s→2.4s），轻微旋转 | accent→#818CF8 | 内部流动 2.4s |
| Speaking | 随 TTS 节奏起伏，边缘波形涟漪 | accent 高亮 | 涟漪+呼吸 |
| Alerting | 浮起放大 + 语义色 + 脉冲 2Hz | warn/danger | 高频脉冲（唯一弹跳态） |

> **M-1 接线状态标注**：六态机 `petMachine.ts` 已实现但**未接入 App.tsx（前端 M-1 实现中）**——现 App.tsx 为二元渲染（alert ? alerting : monitoring），level 1/2 也被渲染为完整提醒态。M-1 按 `docs/specs/design-alert-levels.md` §4 完成接线（ALERT 守卫分级 + context.alertLevel 驱动面板色），接线完成后本标注移除。

### 渐进式打扰（四级）

> **M-1 修订**：完整前端规格见 **docs/specs/design-alert-levels.md**（P0 契约，含各级尺寸/透明度/动效参数/时长总表 + 状态机接线 + 前端验收清单）。核心映射：
> - **level 1/2**：不切换 alerting——仅 `context.alertLevel` 更新 + 面板状态点变色；L2 桌宠单次呼吸加速（scale 1.0→1.03，0.6s，iteration=1）
> - **level ≥3**：ALERT 事件转 alerting——宠物 140px/100%/2Hz 脉冲 + ReminderToast；L4 由后端触发推送，前端同 L3 视觉（语音播报属 V1.1 O-005）

1. 微型状态点变色（不打断）
2. 宠物小幅度移动/颜色变化（余光可见）
3. 宠物浮起放大 + 脉冲（醒目）
4. 语音播报 + 手机推送（高优先级异常）

### 无障碍

- `prefers-reduced-motion`：全部退化静态颜色 + 低透明度变化，保留语义色
- 提醒必须视觉 + 声音双重通道
- 打断立即响应（<100ms）

## 5. 字体

```css
--font-display: "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", sans-serif;
--font-body:    "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", sans-serif;
--font-mono:    "JetBrains Mono", "Cascadia Code", "Consolas", monospace;
```

| 场景 | 字体 | 字号/字重 |
|---|---|---|
| 宠物气泡/状态提示 | body | 12-14px / 400-510 |
| 监控面板标题 | display | 16-18px / 590 |
| 监控数据/状态码/时间 | mono | 12-14px，数字列对齐 |
| 语音转录 | body + mono 混排 | 14-16px |
| 标签/徽章 | body | 11-12px / 510，ALL CAPS 字距 ≥0.06em |

## 6. 图标库（锁定）

**Lucide**（MIT）：16px 行内 / 20px 按钮内 / 24px 独立图标

| 类别 | 图标 |
|---|---|
| 监控 | Eye / Activity / Radio / ScanLine / LayoutGrid |
| 语音 | Mic / MicOff / Volume2 / Pause / Square |
| 状态 | CheckCircle2 / AlertTriangle / XCircle / Info / Loader2 |
| 应用 | Code2 / TerminalSquare / Braces / Bot |
| 通用 | Settings / Bell / ChevronRight / X / Plus |

**禁止 emoji 作功能图标**（P0-1）。宠物透明窗口本体不用图标，图标仅用于控制面板/设置/通知气泡。

## 7. P0 合规自查

- [x] 无 emoji 功能图标（Lucide 锁定）
- [x] 无紫粉渐变（accent #38BDF8，宠物 #2563EB→#38BDF8；Indigo 仅 thinking 纯色）
- [x] 无空洞占位文案（全部具体状态/动作描述）
- [ ] 无硬编码颜色（M-1 修复中，见下方 P0-2 token 化验收）
- [x] 无 AI 模板味（不用玻璃拟态+紫粉渐变+发光边框组合）

### 7.1 P0-2 硬编码色值 token 化验收（M-1 新增）

**验收标准（前端照做）**：
- 除 `tokens.css`（及本 DESIGN.md §2 token 定义处）外，**任何组件/样式不得出现 hex / rgba / rgb 字面量**——一律经 `var(--token)` 引用。
- SVG 渐变 stop 与 fill 一律 `style={{ stopColor: "var(--token)" }}` / `style={{ fill: "var(--token)" }}`（SVG 属性不支持 var()，必须走 style 内联）。
- 全项目静态扫描命令：`grep -rnE "#[0-9a-fA-F]{3,8}|rgba?\(" src --include=*.tsx --include=*.ts`（仅 tokens.css 命中允许）。

**当前违规映射表（Pet.tsx / VoiceOrb.tsx，M-1 须清零）**：

| 文件 | 现值 | 替换 Token |
|---|---|---|
| Pet.tsx `TONE_COLOR.neutral` | `#38bdf8` | `var(--info)` |
| Pet.tsx `TONE_COLOR.success` | `#34d399` | `var(--success)` |
| Pet.tsx `TONE_COLOR.warn` | `#f59e0b` | `var(--warn)` |
| Pet.tsx `TONE_COLOR.danger` | `#f87171` | `var(--danger)` |
| Pet.tsx 高光 stop `#ffffff` | `#ffffff` | `var(--pet-core-hi)` |
| Pet.tsx 高光 ellipse `#ffffff` | `#ffffff` | `var(--pet-core-hi)` |
| VoiceOrb.tsx `PHASE_COLOR.idle/monitoring/listening/speaking` | `#38bdf8` | `var(--info)` |
| VoiceOrb.tsx `PHASE_COLOR.thinking` | `#818cf8` | `var(--thinking)` |
| VoiceOrb.tsx `PHASE_COLOR.alerting` | `#f87171` | `var(--danger)` |
| App.tsx `box-shadow: 0 8px 30px rgba(0,0,0,0.4)` | rgba 字面量 | `var(--toast-shadow)` |

### 7.2 无障碍验收清单（M-1 新增，前端照做）

| # | 项 | 验收标准 |
|---|---|---|
| A1 | pet-anchor 可访问 | `role="button"` + `tabIndex={0}` + `aria-label`（"打开监控面板"/"关闭监控面板"随状态切换）+ `aria-expanded={showPanel}`；`onKeyDown` 支持 Enter/Space 触发；`:focus-visible` 显示 `--focus-ring`；点击区 ≥44×44px |
| A2 | mp-meta 对比度 | 监控面板元数据行（帧数/耗时/时间）：`font-size: 11px → 12px`，`color: var(--muted) → var(--fg-2)`（深色 7.18:1 / 浅色 7.07:1 达标） |
| A3 | ReminderToast 语义 | `role="alert"` + `aria-live="assertive"`；内容含 app_id/state/summary/suggestion |
| A4 | toast 关闭钮 | 关闭钮用 Lucide `X`（20px），`aria-label="关闭提醒"`；键盘可达 |
| A5 | 焦点管理 | toast 打开时焦点移入关闭钮；关闭/8s 回落时焦点归还 pet-anchor；支持 ESC 关闭 |
| A6 | 状态不靠颜色 | 状态点颜色变化的同时保留图标差异（CheckCircle2/AlertTriangle/XCircle，已有），不单靠颜色传达 |
| A7 | reduced-motion | 全局降级已实现（global.css）；四级打扰降级见 specs/design-alert-levels.md §5 |
| A8 | 键盘导航 | 全部交互元素可 Tab 到达 + 可见焦点环；无仅 hover 交互 |

## 8. 交付物映射（Phase 2 产出 · M-1 状态更新）

- [x] pet-ui/src/styles/tokens.css（Token 实现）— 深色已实现；浅色主题契约已定稿（§2.2 CSS 片段），前端 M-1 落地
- [x] pet-ui/src/components/Pet.tsx（宠物光球）— 已实现；M-1 做 token 化（§7.1）+ 四级打扰接入
- [x] pet-ui/src/components/VoiceOrb.tsx（六态光球）— 已实现；M-1 做 token 化（§7.1）
- [x] pet-ui/src/components/MonitorPanel.tsx（监控面板）— 已实现；M-1 做 a11y 修复（§7.2 A2）
- [ ] pet-ui/src/components/ReminderToast.tsx（提醒气泡）— **M-1 拆分**：当前内联于 App.tsx（审计 #10），拆为独立组件并满足 §7.2 A3-A5
- [ ] pet-ui/src/components/Settings.tsx（设置）— **M-1 拆分**：当前内联于 App.tsx（审计 #10），含主题切换（§2.2 data-theme）
- [x] pet-ui/src/state/petMachine.ts（XState 六态机）— 已实现；M-1 接线（ALERT 分级守卫，§4 标注）
- [x] pet-ui/src/state/wsClient.ts（WS 客户端）— 已实现
- [ ] pet-ui/src/assets/lottie/（光球动画 JSON）— **皮肤扩展预留**：V1 不交付（纯代码 SVG），空目录保留 .gitkeep（§3 处置标注）
