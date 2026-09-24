# 桌面端（pet-ui）商业化重做 — 验收记录

> 2026-09-25。规格：`docs/design/2026-09-24-desktop-pet-commercial-redesign.md`（§7 为 7 条验收标准）。
> 触发：用户「不要给我修修补补。必须按照最专业的商业化标准重做」。
> 结论：**7 条全部 PASS**，但有 5 项未验证 + 1 项透明度提示 + 1 条关于"谁看了图"的限定，见下。

---

## 1. 做了什么（两类交付）

**插画（`components/Pet.tsx` 261 行 + `styles/pet.css` 153 行）**
从"80px 手写几何猫脸"重做为**完整猫体**：`viewBox 0 0 240 240`、默认 `sizePx 152`；
图层含地面接触阴影、卷尾、躯干、前爪（含趾间分隔）、头（受光/背光两档）、内外耳、分层眼（眼白/虹膜/瞳孔/高光）、
鼻、上唇分叉嘴、6 根两端收细的锥形胡须、项圈 + 品牌绿吊牌；描边粗细随部位变化（外轮廓 2.5 / 内部 1.5）。
五态由**姿态与动画类**驱动（呼吸/眨眼/耳尖、聆听耳前倾+瞳孔放大+光晕、思考头侧倾+三点、说话嘴开合+起伏、提醒耳后压+语义色环），
不是"换描边色"。`pet-shell` 改为径向渐变填充且 idle 默认 `opacity:0`，只在语音/提醒态显形。

**应用壳（`App.tsx` 300 行 + 新增 `ControlDock.tsx` / `shell.css` / `voice-orb.css`）**
状态胶囊（lucide 图标 16px）、40×40 图标按钮（三态齐全）、白底面板卡片、
**故障横幅重做**（浅红底 + 1px 描边 + 图标 + 主/次动作，不再是压满窗口的 2px 纯红）；
保留窗口尺寸契约（200/380/400/440）与 `controlsHidden` 三态交互模型。

**关键集成修正（team-lead 修）**：`App.tsx` 原本传 `sizePx={isAlerting ? 140 : 96}` ——
产品实际渲染的是这个显式传参，**新插画会被渲染成 96px**（规格要 152）。已改为 **176/152**。

## 2. 七条验收

| # | 标准 | 结论 | 证据 |
|---|---|---|---|
| 1 | 200×200 透明窗宠物完整/居中/无糊边 | PASS | `outputs/2026-09-25-pet-ui-redesign/screenshots/verify/idle_200_{light,dark,brand}.png`（IHDR 核对 200×200） |
| 2 | 152px 与 380×480 面板态 | PASS | `.../idle_152_light.png`(152×152)、`.../p_monitor_380x480.png`(380×480) |
| 3 | 五态各一张且非"只改描边色" | PASS | `.../st_{idle,listening,thinking,speaking,alerting}.png` + `pet.css` 状态类 + 测试断言 |
| 4 | 全仓 emoji=0 / 非 token 色=0 / 内联 `<style>`=0 | PASS | `node pet-ui/verify/verify-acceptance.mjs pet-ui/src` → `VERDICT: PASS` |
| 5 | `vitest` 全绿且 `Pet.test.tsx` 语义不变 | PASS | `Tests 16 passed (16)`（Pet 11 + App 5） |
| 6 | `npm run build` 通过 + 截图归档 | PASS | `vite build ✓ built`、`tsc` 0 错；截图见上 |
| 7 | 非实现者逐条 pass/fail | PASS | 独立 verifier 报告（7 条 + 两组反向验证） |

**反向验证（防假绿，已执行）**
- 扫描器：临时放入 `<style>` + emoji + 非 token 色 ⇒ 扫描**转红**；删除后**恢复绿**。扫描器还刻意用**双模式**（剥注释保留 JSX 属性扫颜色 / 剥字符串扫标签与 emoji），避免 `fill="#ff0000"` 被当字符串误杀。
- token 篡改：把 `--pet-outline` 改成 `#ff0000` ⇒ 截图中**描边真的变红**（排除缓存假绿）；还原后 sha256 与基线**逐字节一致** `0c2f4bd0…6675b73`。

**未篡改证明**：`git diff -- pet-ui/vite.config.ts` 为空；产品源 sha 全程不变（`Pet.tsx` `37c29dec…`、`pet.css` `c3a80cc1…`、`design-tokens.css` `0c2f4bd0…`）。

## 3. ⚠️ 必须说清的三件事

**(a) 谁看了图。** 独立 verifier **无法查看图片**（其模型无图像输入，Read 图片被过滤），
所以它给出的第 1/2/3 条视觉 PASS **依据的是 PNG IHDR 尺寸核对 + 组件结构 + 测试断言 + 代码逻辑**，不是像素级目检。
**像素级目检由 team-lead 完成**（team-lead 非实现者），依据为多背景合成图与定尺寸 iframe 渲染图。

**(b) `shell.css` 1047 行**（透明度提示，非硬违约）：§6 的 300 行契约原文限定 "JavaScript modules"，
样式表不在该硬约束内。但 1047 行的单一样式表是**可维护性欠债**，建议后续按组件拆分。

**(c) 两次"仪器造出来的假结论"（本会话教训，已写进 `measurement-integrity-audit` 技能）**
1. 端口扫描用 `settimeout()` + `connect_ex()` 恒返 10035 ⇒ "无开放端口"是**假阴性**；
2. headless Chrome 的 `--window-size` **不等于布局视口**（请求 440×220 实测视口 512×122，还把视口缩放进图）⇒
   我据此报过一条**并不存在**的"猫被窗口右边缘切掉"缺陷，已撤回。
   **铁律：测"某尺寸下的布局"必须先把视口钉死（定尺寸 iframe 或 CDP），并每次先跑探针页确认视口再信截图。**

## 4. 未验证项（如实列出，不给假 pass）

1. **真实 Tauri 窗口 resize 行为**：headless 无法启动 Tauri 运行时；`set_pet_size` 分支未在真实窗口跑过。
2. **透明窗口叠加真实桌面的观感**：只能截固定尺寸画布，无法验证叠加到实际壁纸上的效果。
3. **面板与真实后端的联动**：面板用 mock fetch 渲染，未连真实后端验证接口契约/鉴权。
4. **动画动态观感**：仅静态截图 + CSS 动画类存在性 + 测试断言，未做帧级动态比对。
5. **idle 且无后端"干净态"的应用级合成**：浏览器里必然进 fault 态并弹故障横幅，故 200×200 的 idle 应用级截图尚缺
   （猫本身在 152/200 尺寸已单独验证）。

## 5. 证据与复核入口

- 证据目录（**被 gitignore**，不随仓库发布）：`outputs/2026-09-25-pet-ui-redesign/`
  （`screenshots/` 37 项 + `harness/` 出图页 + `README.md` 说明）
- 机械扫描（第 4 条）：`node pet-ui/verify/verify-acceptance.mjs pet-ui/src`
- ⚠️ harness **未接进产品构建**（`vite.config.ts` 保持与 HEAD 一致）；重新出图需临时加 rollup 输入，**出完务必还原**。
