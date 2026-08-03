# ADR-002: Windows 窗口截屏方案 — Windows.Graphics.Capture

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远

## 背景

需按进程捕获 Codex / Trae / Hermes 三个桌面窗口。Trae 是 Chromium(Electron) GPU 渲染窗口，传统 GDI 截图方案会黑屏。目标：单窗口捕获、GPU 窗口兼容、低延迟低 CPU。

## 选项对比

| 方案 | 单窗口 | GPU 窗口(Chromium) | 延迟/CPU | 结论 |
|---|---|---|---|---|
| Windows.Graphics.Capture（WGC） | 是（HWND/窗口名） | 是（关键） | ~6ms/10% | 选中 |
| DXGI Desktop Duplication | 否（整屏） | 是 | ~8ms/12% | 兜底 |
| PrintWindow | 是 | 否（GPU 窗口黑屏） | ~35ms/15% | 否决（仅老 GDI 窗口） |
| mss+PIL（BitBlt） | 区域 | 否 | ~12ms/8% | 兜底 |

## 决策

- 主路径：**windows-capture 2.0.0**（PyPI 2026-04-15，Rust+Python，支持 window_name 捕获指定窗口、事件式帧回调、内置 DXGI Desktop Duplication API）
- API：`WindowsCapture(window_name=...)` + `@capture.event on_frame_arrived(frame, capture_control)` + `frame.save_as_image(path)`
- 兜底：DXGI Desktop Duplication（窗口最小化/空帧时按显示器裁剪）

## 后果

- 正面：唯一能稳定捕获 Trae(Chromium) 的方案；事件式回调适配 asyncio 轮询
- 负面：WGC 首次运行需用户通过系统选择器对每个目标窗口授权一次（黄框提示）——属透明可审计行为，符合"只监控不操控"
- 已知坑：窗口最小化时 WGC 可能返回空帧 → DXGI 兜底或仅窗口状态监控
- 替代触发条件：PoC B2 某窗口授权失败或黑屏 → 该窗口降级"仅窗口状态监控"；全败 → 进程活跃度 + 日志尾部 + 30s 低频截屏混合
