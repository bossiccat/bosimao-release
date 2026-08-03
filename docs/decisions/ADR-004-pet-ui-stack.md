# ADR-004: 桌宠 UI 技术栈 — Tauri v2 + React + Lottie

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远

## 背景

需要 Windows 桌面常驻的宠物形 UI：透明无边框窗口 + 动画 + 系统托盘 + 点击穿透，常驻内存要求低（12G 显存机器，GPU 资源留给模型）。

## 选项对比

| 方案 | 透明窗口 | 动画 | 托盘 | 常驻内存 | 结论 |
|---|---|---|---|---|---|
| Tauri v2 | 支持（transparent+点击穿透） | Web 动画（SVG/Lottie） | 原生 SystemTray | 20-60MB | 选中 |
| WPF | 支持（AllowsTransparency） | Lottie-Windows | NotifyIcon | 50-80MB | 备选（团队 C# 熟） |
| Electron | 支持 | Web 动画 | tray | 150-200MB+ | 否决（常驻太重） |
| WinForms | 弱 | 弱 | NotifyIcon | 低 | 否决 |
| Unity | 支持 | 强 | 需插件 | 300MB+ | 否决（过重） |

## 决策

- **Tauri v2.11.5**（2026-07-01）+ `@tauri-apps/cli 2.11.4` + `@tauri-apps/api 2.11.1`
- 前端：React + Vite + TypeScript
- 动画：Lottie-web / 自定义 SVG（光球动效）
- 状态机：XState（六态机 petMachine.ts）
- 窗口配置：`transparent=true` / `decorations=false` / `alwaysOnTop` / `skipTaskbar`；点击穿透 `set_ignore_cursor_events`

## 后果

- 正面：常驻 20-60MB；Web 技术栈动画表达力强；透明+穿透+托盘全支持（BongoCat 已验证 Tauri 实现桌宠）
- 负面：需 Rust 工具链；点击穿透与交互需精细切换
- 替代触发条件：团队 Rust 熟练度不足 → WPF 备选（成本约 +30%）
