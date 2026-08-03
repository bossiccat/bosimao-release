# Phase 1 设计方向调研 — 宠物形 AI 智能助手

> 设计师：颜好看 | 日期：2026-08-02 | 团队：monitor-agent-project
> 任务：#3 设计师调研设计方向 | 状态：完成

---

## 0. 设计寄存器判断（Step 1 必判）

- **寄存器**：Product 为主 + 宠物层品牌情感。核心界面（监控面板/语音对话/设置）走 Product 寄存器（赢得熟悉感，标杆 Linear/Vercel/Mico）；宠物形象层允许品牌级表现力（情感载体）。
- **三轴刻度**：`DESIGN_VARIANCE=5`（UI 面板克制对称，宠物本体允许有机非对称）/ `MOTION_INTENSITY=6`（宠物是核心动效载体，监控提醒克制）/ `VISUAL_DENSITY=5`（监控信息适中）。
- **平台轴**：Windows 桌面透明窗口（常驻角落/任务栏附近），深色环境为主。

---

## 1. 设计参考清单（三类调研）

### 类别 A：桌面宠物应用
| 参考 | 类型 | 可借鉴点 |
|---|---|---|
| [CodePet](https://gitee.com/Badguy_zzy/code-pet) | Tauri 桌宠 | 透明无边框窗口 / 鼠标穿透 / 5 态动画（idle/click/drag/walk/sleep）/ 右键菜单工作台 |
| [蓝色小嗵 Tongluv](https://meta.appinn.net/t/topic/86119) | Windows 桌宠 | 眼睛跟随光标 / 窗口吸附趴伏 / 情绪气泡（配色随心情变化）/ **透明度 20–100% 可调**（工作时调低不遮挡） |
| [StepClaw 阶跃龙虾](https://zhidx.com/news/42852.html) | **效率型 AI 桌宠（最重要对标）** | 桌面悬浮窗常驻 + **任务进度实时可视化** + 触发器主动执行 + 本地存储 |
| [Star 桌面 Agent](https://ithub.global.ssl.fastly.net/dulaiduwang003/star-agent) | 桌面宠物 Agent | **纯代码绘制（零贴图素材）** / 情绪与亲密度 / 能操控电脑的本地 Agent |

**关键洞察**：现有桌宠多为"可爱陪伴型"（像素猫/Live2D），本项目是**效率监控型 AI 助手**。StepClaw 的产品形态（悬浮窗常驻 + 进度可视化 + 主动提醒）与本项目最接近；Star 的"纯代码绘制零贴图"对透明窗口性能最有启发。

### 类别 B：AI 助手形象设计
| 参考 | 品牌 | 可借鉴点 |
|---|---|---|
| **Mico**（Copilot 光球） | Microsoft | **最重要对标**：抽象光球/无固定形状/随对话情绪实时变色/语音交互状态（聆听柔和蓝光脉冲、思考复杂变色、回应流畅动画、空闲呼吸）/ 选择性开启回避 Clippy 覆辙 / 回避恐怖谷 |
| Claude 星芒 | Anthropic | 差异化配色（赤陶橙 #d97757，全盘不用蓝）/ 手绘感非对称星芒 / "说不清是什么但有生命力" |
| OpenAI blossom + 脉冲蓝圆盘 | OpenAI | 语音视觉表现用"脉冲蓝圆盘"（Studio Dumbar 设计）/ 六环互锁 |
| Lil' Finder Guy | Apple | 蓝白小矮人 / 品牌角色成为情感锚点 |
| Haektaegi | Naver | 史莱姆/果冻造型 / 成长即价值具象化 |

**关键洞察**：AI 吉祥物时代已至（微软/苹果/Naver 同步推角色）——"当 AI 产品看不见，吉祥物成了给智能体一张熟悉的 face 的战略"。光球是语音 AI 的通用视觉语言（Siri/ChatGPT/Mico/Gemini 均用），用户熟悉、信任成本低。

### 类别 C：语音交互界面
| 参考 | 类型 | 可借鉴点 |
|---|---|---|
| [Siri 波形](https://github.com/kopiro/siriwave) | 波形动画 | 求和阻尼正弦波 / 幅度由语音驱动 |
| [ChatGPT orb](https://gunbark.dev/content/6daa37ee-cf2f-433b-a86f-0ff5dc3812a9) | 全屏呼吸光球 | 蓝白流动随对话状态变形 |
| [Voice Assistant Orb 四态机](https://fwdtools.com/ui-snippets/voice-assistant-orb) | 语音光球 | **Idle→Listening→Thinking→Speaking 状态机** / 声呐脉冲环 / 打断（barge-in）取消安全 |
| [Rive AI 助手设计](https://dev.to/uianimation/how-to-design-an-ai-assistant-ui-using-rive-orbs-avatars-42ci) | 状态机设计 | Orb vs Avatar 决策框架 / 状态机结构（Idle/Listening/Thinking/Speaking/Success/Error） |

**核心设计原则（蒸馏，最重要）**：
1. **Idle 永不静止**：低幅度自主呼吸运动（有生命感）
2. **物理感优于字面**：快攻击（fast attack）、慢阻尼衰减（damped decay）→ 读起来像"一个身体在被移动"，而非 VU 表
3. **状态改变是材质/运动的改变，不是颜色改变**：重量/缩放/运动/深度，而非纯色相（单形态连续变换，不换图标）
4. **多频段响应**：语音信号分解为音节级脉冲 vs 短语级能量，对应不同物理反应
5. **打断 = 变形不重置**：barge-in 时动画被"压扁"而非"重开"
6. **边缘克制**：过度动画读作焦虑，Idle 幅度应几乎不可感知

---

## 2. 对标品牌 + 设计语言

**对标品牌**：
1. **Microsoft Copilot Mico** — 语音交互 + 光球形象 + 状态变色（交互层对标）
2. **StepClaw 阶跃龙虾** — 效率型 AI 桌宠，悬浮窗常驻 + 进度可视化（产品形态对标）
3. **Claude / Anthropic** — 差异化配色策略 + 有机品牌符号（品牌差异化对标）
4. **Linear / Vercel** — 开发者工具的设计工艺基准（UI 质感对标）

**设计语言定调：「有机科技 · 冷静陪伴」(Organic Tech, Calm Companion)**

风格关键词（物理对象词）：**发光的活体传感器 / 显微镜下的荧光细胞 / 深空里的一颗小行星**
- 深色开发者环境（非纯黑，带冷蓝调）— Linear/Vercel 质感
- 宠物 = 有机光体（柔和边缘 + 核心高光，像"有生命的传感器"）
- UI 面板 = 克制深色 + 1px 边框 + 低阴影（Product 寄存器）
- 动效 = 克制、有物理感（快攻击慢衰减）、状态靠材质/运动表达
- **反 AI 模板**：不用紫粉渐变、面板不用装饰性毛玻璃（透明窗口本身是功能需要）、不用发光边框堆砌

---

## 3. 配色方向（Design Token 草案）

> 基于 color-palettes.md 第 1 套（SaaS 信任蓝）+ ai-native.md 规范，深色适配。**全部通过 Token 引用，不硬编码**。

### 深色主题（默认，开发者环境）

**A1-identity**
| Token | 值 | 角色 |
|---|---|---|
| `--bg` | `#0B0E14` | 深空蓝黑（带蓝调，非纯黑） |
| `--surface` | `#12161F` | 面板/弹窗 |
| `--surface-2` | `#1A202C` | 悬浮层/下拉 |
| `--fg` | `#E6EAF2` | 主文本（冷调白） |
| `--fg-2` | `#9AA4B2` | 次级文本 |
| `--muted` | `#6B7686` | 弱化文本 |
| `--accent` | `#38BDF8` | **品牌/宠物核心光色（天蓝，AI 智能感）** |
| `--border` | `rgba(255,255,255,0.08)` | 默认边框 |
| `--border-soft` | `rgba(255,255,255,0.05)` | 内部行分隔 |

**A2-semantic（监控状态语义色 — 本项目核心）**
| Token | 值 | 语义 |
|---|---|---|
| `--success` | `#34D399` | **有进展**（Agent 正常推进） |
| `--warn` | `#F59E0B` | **卡住/停滞**（需关注） |
| `--danger` | `#F87171` | **跑偏/异常**（需干预） |
| `--info` | `#38BDF8` | 信息/对话（与 accent 同族） |
| `--focus-ring` | `rgba(56,189,248,0.4)` | 焦点环 |

**宠物本体光色（同色系深浅渐变，允许）**：`#2563EB → #38BDF8`（深蓝→天蓝，**非紫粉、非 Indigo→Pink**）

### 浅色主题（备用，设置面板可切）
- `--bg` `#F6F8FB` / `--surface` `#FFFFFF` / `--fg` `#1A2230` / `--muted` `#64748B` / `--accent` `#0EA5E9` / `--border` `#E2E8F0`
- 语义色：success `#059669` / warn `#D97706` / danger `#DC2626`

### 强调色使用规则
- 每屏 ≤2 处 accent；监控面板主色只出现在"宠物本体 + 当前状态指示"，其余 UI 用中性色
- 语义色仅用于状态点/通知徽标，不扩散到大面积

---

## 4. 宠物形象方向（2-3 个 + 推荐）

### 双态设计要求（贯穿所有方向）
- **监控态（低调）**：小尺寸（约 64–96px）、低透明度（20–40%，Tongluv 参考）、贴边/角落、低幅呼吸，不遮挡视线
- **提醒态（醒目）**：浮起放大（至 120–160px）、透明度拉满、颜色切语义色、脉冲 2Hz + 可配语音/推送

### 方向 1：有机光球（Mico 路线）⭐ 推荐
- 一团流动的光，有"核"（核心高光似眼睛）+ 柔和能量外壳，边缘羽化
- 监控态：淡蓝微光几乎不可感知；提醒态：变亮 + 颜色切换 + 脉冲
- **理由**：光球是语音 AI 通用语言（Siri/ChatGPT/Mico/Gemini），用户信任成本最低；与语音状态机（聆听/思考/说话都是光形态变化）天然契合；径向渐变 + 少量粒子渲染开销最低（RTX 3060 常驻友好）；纯代码绘制可行（Star 已验证零贴图）

### 方向 2：机械核心/浮游哨兵（开发者向）
- 六边形核心 + 雷达眼 + 微型状态灯带，像"盯着你 Agent 的小哨兵"
- 监控态：安静的六边形核心缓慢旋转（雷达扫描）；提醒态：核心亮起 + 状态灯闪烁
- **理由**：开发者气质强，与"监控 AI 编程应用"主题高度契合；视觉差异化明显（避开所有光球同质化）
- **代价**：几何硬边 + 灯带细节在 64px 小尺寸下辨识度下降；情感连接弱、偏冷

### 方向 3：抽象几何生命体（Claude 星芒/史莱姆路线）
- 手绘感非对称星芒/软体水滴，说不清是什么但有生命力
- 监控态：蜷缩小憩的软体生物；提醒态：睁眼/伸触角/变色
- **理由**：差异化最强、品牌记忆点最高（Claude 证明"不说人话的符号"反而成为资产）
- **代价**：动画制作成本最高；对小尺寸 + 低透明度场景不友好；与"智能助手"的联想不如光球直接

### 推荐结论
**主推方向 1（有机光球）**，融合方向 3 的"拟态表情"元素（核心高光可做出睁眼/闭眼/关注等微表情），方向 2 作为"硬核开发者皮肤"备选（皮肤系统可扩展）。给宠物一个名字（如"星核 Spark"）增强情感连接。命名/文案避免空洞（P0-3）。

---

## 5. 语音交互状态设计（状态机 + 六态）

> 基于调研蒸馏原则：Idle 永不静止 / 状态靠材质运动而非纯颜色 / 单形态连续变换 / barge-in 变形不重置 / reduced-motion 降级

### 状态机
```
Monitoring（常驻） ──唤醒──▶ Listening ──输入结束──▶ Thinking ──响应──▶ Speaking
      ▲                                                                  │
      └──────────────── 打断(barge-in) ──任意态──▶ Listening ◀──────────┘
      ▲                          │
      └──── 空闲回落 ◀───────────┘
Monitoring ──事件──▶ Alerting（提醒）──确认/关闭──▶ Monitoring
```

### 六态视觉规范
| 状态 | 视觉表现 | 颜色 | 动效参数 |
|---|---|---|---|
| **Idle 待机** | 宠物缩小贴边，核心缓慢呼吸 | accent 20% 亮度 | scale 1.0→1.02，6s 周期 |
| **Monitoring 监控中** | 核心眼缓慢扫描，头顶微型状态点（三个 AI 应用） | accent + 语义点 | 8s 低频，不打扰 |
| **Listening 聆听中** | 光球微放大，外层声呐脉冲环（border-only 扩散，幅度跟随用户音量） | accent 渐亮 | 脉冲环 0.8s，幅度=语音 |
| **Thinking 思考中** | 内部流动加速（6s→2.4s），轻微旋转，无脉冲环 | accent → 靛 #818CF8（纯色） | 内部流动 2.4s |
| **Speaking 说话中** | 光球随 TTS 语音节奏起伏，边缘波形涟漪 | accent 高亮 | 涟漪 + 呼吸 |
| **Alerting 提醒中** | 宠物浮起放大 + 颜色切语义色 + 脉冲 2Hz | warn/danger | 高频脉冲（唯一允许弹跳态） |

### 渐进式打扰（四级，避免打扰）
1. 微型状态点变色（不打断）
2. 宠物小幅度移动/颜色变化（余光可见）
3. 宠物浮起放大 + 脉冲（醒目）
4. 语音播报 + 手机推送（高优先级异常）

### 无障碍
- `prefers-reduced-motion`：全部退化静态颜色 + 低透明度变化，保留语义色
- 提醒必须配视觉 + 声音双重通道（不只靠颜色，10 级优先级规则）
- 打断立即响应（<100ms）

---

## 6. 字体方向

> Windows 桌面应用（非 Web），不用 Google Fonts CDN；中文用系统字体保证渲染质量，数字/状态用等宽字体（调研：等宽数字列对齐 + 开发者气质 + 状态码辨识）。

```css
/* 三层字体栈 */
--font-display: "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", sans-serif;
--font-body:    "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", sans-serif;
--font-mono:    "JetBrains Mono", "Cascadia Code", "Consolas", monospace;   /* 内嵌 JetBrains Mono */
```

| 场景 | 字体 | 字号/字重 |
|---|---|---|
| 宠物气泡/状态提示 | body（系统） | 12–14px / 400–510 |
| 监控面板标题 | display（系统） | 16–18px / 590 |
| 监控数据/状态码/时间 | **mono（JetBrains Mono）** | 12–14px / 400–510，数字列对齐 |
| 语音转录 | body + mono 混排（代码/数字走 mono） | 14–16px / 400 |
| 标签/徽章 | body | 11–12px / 510，ALL CAPS 字距 ≥0.06em |

---

## 7. 锁定的 SVG 图标库

**锁定：Lucide**（MIT，ai-native.md 规范同款：Sparkles/Brain/Wand2 等）

- 尺寸规范：**16px 行内 / 20px 按钮内 / 24px 独立图标**（全项目统一）
- 图标语义映射（控制面板/设置页/通知气泡使用；宠物透明窗口本体不用图标）：
  - 监控：`Eye` `Activity` `Radio` `ScanLine` `LayoutGrid`
  - 语音：`Mic` `MicOff` `Volume2` `Pause` `Square`（打断）
  - 状态：`CheckCircle2` `AlertTriangle` `XCircle` `Info` `Loader2`（spin）
  - 应用：`Code2` `TerminalSquare` `Braces` `Bot`
  - 通用：`Settings` `Bell` `ChevronRight` `X` `Plus`
- **禁止 emoji 作功能图标**（P0-1）：宠物气泡内可用文字/图标，不用 🚀📊✨ 等

---

## 8. P0 合规自查

- [x] **P0-1 无 emoji 功能图标**：锁定 Lucide，尺寸 16/20/24px
- [x] **P0-2 无紫粉渐变**：accent 天蓝 #38BDF8，宠物本体同色系蓝渐变 #2563EB→#38BDF8；Indigo #818CF8 仅作为 thinking 态纯色（允许）
- [x] **P0-3 无空洞文案**：全部文案为具体动作/状态描述（如"Codex 会话卡住 3 分钟"，非"Welcome"）
- [x] **无硬编码颜色**：全部 Design Token 引用
- [x] **无 AI 模板味**：不用玻璃拟态+紫粉渐变+发光边框组合；面板克制 Linear 风

---

## 9. 交付物清单

- [x] 本调研文档（`docs/design-research-phase1.md`）
- [x] 设计参考清单（3 类 × 多参考，含链接）
- [x] 对标品牌 + 设计语言定调
- [x] 配色方向（A1/A2 Token 草案，深/浅双主题）
- [x] 宠物形象 3 方向 + 推荐
- [x] 语音交互六态设计 + 状态机
- [x] 字体方向（系统字体 + JetBrains Mono）
- [x] SVG 图标库锁定（Lucide）

> Phase 2 由 DESIGN.md 9 节模板产出完整设计契约（token-standard.md §10）
