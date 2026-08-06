"""PoC-B2 核心：窗口工具 + CaptureRun（WGC 帧采样统计）"""
from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path

import numpy as np

try:
    import win32gui
    import win32con
except ImportError:
    win32gui = win32con = None

from windows_capture import WindowsCapture, Frame, InternalCaptureControl


def find_hwnd_by_title(title: str) -> int | None:
    if win32gui is None:
        return None
    res = []

    def cb(hwnd, _e):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            res.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return res[0] if res else None


def restore_window(hwnd: int) -> None:
    if win32gui is None or hwnd is None:
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.4)
    except Exception:
        pass


def minimize_window(hwnd: int) -> None:
    if win32gui is None or hwnd is None:
        return
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    except Exception:
        pass


def is_minimized(hwnd: int) -> bool:
    if win32gui is None or hwnd is None:
        return False
    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False


def cover_window(cover_hwnd: int, target_hwnd: int) -> None:
    """把 cover 窗口移到 target 正上方并置顶，模拟遮挡"""
    if win32gui is None or cover_hwnd is None or target_hwnd is None:
        return
    try:
        l, t, r, b = win32gui.GetWindowRect(target_hwnd)
        w, h = (r - l) // 2, (b - t) // 2
        win32gui.SetWindowPos(
            cover_hwnd, win32con.HWND_TOPMOST, l, t, w, h, win32con.SWP_SHOWWINDOW
        )
        win32gui.SetWindowPos(cover_hwnd, win32con.HWND_TOP, l, t, w, h,
                              win32con.SWP_SHOWWINDOW)
    except Exception:
        pass


def uncover_window(cover_hwnd: int) -> None:
    if win32gui is None or cover_hwnd is None:
        return
    try:
        win32gui.SetWindowPos(cover_hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception:
        pass


class CaptureRun:
    def __init__(self, title: str, out_dir: Path, max_width: int = 1280):
        self.title = title
        self.out_dir = out_dir
        self.max_width = max_width
        self.frames: list[dict] = []
        self.timeline: list[tuple[float, str]] = []
        self._t0 = time.time()
        self._capture: WindowsCapture | None = None

    def _log(self, ev: str) -> None:
        self.timeline.append((round(time.time() - self._t0, 2), ev))

    def _handle_frame(self, frame: Frame, ctl: InternalCaptureControl):
        now = time.time()
        w, h = frame.width, frame.height
        buf = frame.frame_buffer
        black = False
        try:
            if buf is not None and buf.size:
                arr = np.asarray(buf)
                black = bool(np.mean(arr) < 4.0)
        except Exception:
            black = False
        empty = w <= 1 or h <= 1
        rec = {"t": now, "w": w, "h": h, "empty": empty, "black": black,
               "save_ms": None, "downsample_ms": None, "path": None}
        t_save = time.time()
        path = self.out_dir / f"f{len(self.frames):04d}.png"
        frame.save_as_image(str(path))
        t_down0 = time.time()
        try:
            from PIL import Image
            with Image.open(path) as im:
                if im.width > self.max_width:
                    ratio = self.max_width / im.width
                    im = im.resize((self.max_width, max(1, int(im.height * ratio))),
                                   Image.LANCZOS)
                im.save(path.with_suffix(".ds.png"), "PNG")
        except Exception:
            pass
        t_end = time.time()
        rec["save_ms"] = (t_down0 - t_save) * 1000
        rec["downsample_ms"] = (t_end - t_down0) * 1000
        rec["path"] = str(path)
        self.frames.append(rec)
        self._log(f"frame {len(self.frames)} {w}x{h} black={black} empty={empty}")

    def start(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._capture = WindowsCapture(
            window_name=self.title, cursor_capture=None, draw_border=None
        )

        @self._capture.event
        def on_frame_arrived(frame: Frame, ctl: InternalCaptureControl):
            self._handle_frame(frame, ctl)

        @self._capture.event
        def on_closed(ctl: InternalCaptureControl):
            self._log("on_closed")

        threading.Thread(target=self._capture.start, daemon=True,
                         name=f"wgc-{self.title}").start()

    def stop(self):
        # windows-capture 无公开 stop；捕获线程为 daemon，进程退出即停。
        pass

    def wait_frames(self, n: int, timeout: float) -> bool:
        t_end = time.time() + timeout
        while time.time() < t_end:
            if len(self.frames) >= n:
                return True
            time.sleep(0.05)
        return len(self.frames) >= n

    def stats(self) -> dict:
        fs = self.frames
        if not fs:
            return {"frames": 0}
        intervals = [(fs[i]["t"] - fs[i - 1]["t"]) * 1000 for i in range(1, len(fs))]
        dims = {f"{f['w']}x{f['h']}" for f in fs}
        empty = sum(1 for f in fs if f["empty"])
        black = sum(1 for f in fs if f["black"])
        valid = len(fs) - empty
        saves = [f["save_ms"] for f in fs if f["save_ms"] is not None]
        downs = [f["downsample_ms"] for f in fs if f["downsample_ms"] is not None]
        return {
            "frames": len(fs),
            "empty": empty,
            "black": black,
            "valid_rate": valid / len(fs) * 100 if fs else 0,
            "dims": sorted(dims),
            "resolution_consistent": len(dims) == 1,
            "interval_avg_ms": round(statistics.mean(intervals), 1) if intervals else None,
            "interval_min_ms": round(min(intervals), 1) if intervals else None,
            "interval_max_ms": round(max(intervals), 1) if intervals else None,
            "interval_p95_ms": round(sorted(intervals)[int(len(intervals) * 0.95) - 1], 1)
            if intervals else None,
            "save_avg_ms": round(statistics.mean(saves), 1) if saves else None,
            "save_max_ms": round(max(saves), 1) if saves else None,
            "downsample_avg_ms": round(statistics.mean(downs), 1) if downs else None,
            "single_frame_total_avg_ms": round(
                statistics.mean([(f["save_ms"] or 0) + (f["downsample_ms"] or 0)
                                 for f in fs if f["save_ms"] is not None]), 1)
            if fs else None,
        }
