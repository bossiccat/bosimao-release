# POC-002: WGC 三窗口捕获验证（风险②）

> 状态：待执行 | 判定人：架构师 高见远

## 目标

验证 windows-capture 2.0.0 能稳定捕获 Codex / Trae / Hermes 三个窗口（Trae 为 Chromium GPU 窗口，不得黑屏）。

## 步骤

```python
# scripts/poc_002_capture.py
from windows_capture import WindowsCapture, Frame, InternalCaptureControl

def run_capture(window_title: str):
    capture = WindowsCapture(
        window_name=window_title,   # 或 window_hwnd=... 按 hwnd
        cursor_capture=None,
        draw_border=None,
    )

    @capture.event
    def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
        frame.save_as_image("frame.png")

    capture.start()
```

1. 对 Codex（终端）/ Trae（Chromium GPU）/ Hermes 分别建常驻会话
2. 首次运行完成 WGC 授权（系统弹窗点"允许"）
3. 每窗口采样 60 帧：统计帧间隔 / 空帧比例 / 分辨率一致性
4. 覆盖场景：可见 / 最小化 / 遮挡
5. 记录最小化恢复后是否 3s 内自动续帧

## 通过标准

| 指标 | 目标 |
|---|---|
| 可见态 60 帧有效帧率 | ≥ 95%（Trae 不黑屏） |
| 最小化恢复 | 3s 内自动续帧（DXGI 兜底或 WGC 自恢复） |
| 单帧捕获+降采样+存 PNG | ≤ 200ms |

## 失败备用（B 计划）

1. Trae 黑屏 → DXGI Desktop Duplication 按显示器裁剪
2. 授权失败 → 授权引导弹窗 + 手动重试；该窗口降级"仅窗口状态监控"
3. 全败 → 混合方案（进程活跃度 + 日志尾部 + 30s 低频截屏）；判定②不通过则 V1 缩为单窗口验证

## 结论记录

- [ ] 通过（记录三窗口实测数据）
- [ ] 降级（记录采用方案）
