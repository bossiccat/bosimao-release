"""PoC-B2 冒烟：WGC 单窗口捕获（3 秒）— 验证授权与基本出帧"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from windows_capture import WindowsCapture, Frame, InternalCaptureControl


def smoke(title: str, seconds: float = 3.0) -> dict:
    stats = {"frames": 0, "dims": set(), "first_frame_at": None, "error": None}
    t0 = time.time()
    try:
        capture = WindowsCapture(window_name=title, cursor_capture=None, draw_border=None)
    except Exception as e:  # noqa: BLE001
        stats["error"] = f"构造失败: {e}"
        return stats

    @capture.event
    def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
        now = time.time()
        if stats["first_frame_at"] is None:
            stats["first_frame_at"] = now - t0
        stats["frames"] += 1
        stats["dims"].add(f"{frame.width}x{frame.height}")
        if now - t0 >= seconds:
            capture_control.stop()

    @capture.event
    def on_closed(capture_control: InternalCaptureControl):
        capture_control.stop()

    # 主线程 10s 硬超时：授权弹窗/阻塞时不挂死
    timer = threading.Timer(10.0, lambda: (_ for _ in ()).throw(SystemExit("timeout")))
    timer.daemon = True
    try:
        capture.start()
    except BaseException as e:  # noqa: BLE001
        stats["error"] = stats["error"] or f"运行异常: {e}"
    finally:
        timer.cancel()
    stats["elapsed"] = round(time.time() - t0, 1)
    return stats


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "ChatGPT"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    s = smoke(title, secs)
    print(f"title={title!r}")
    for k, v in s.items():
        print(f"  {k}: {v}")
