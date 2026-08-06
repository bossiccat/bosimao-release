"""窗口定位：进程名 → hwnd（psutil + win32）"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)

try:
    import win32gui
    import win32process
except ImportError:  # 非 Windows 环境降级
    win32gui = None
    win32process = None


@dataclass(frozen=True)
class WindowRect:
    """窗口屏幕坐标区域（win32gui.GetWindowRect）"""

    left: int
    top: int
    width: int
    height: int


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process_name: str
    rect: WindowRect | None = None


def _find_windows_by_pid(pid: int) -> list[int]:
    if win32gui is None or win32process is None:
        return []
    results: list[int] = []

    def _enum_cb(hwnd: int, _extra: object) -> None:
        if win32gui.IsWindowVisible(hwnd):
            _, wnd_pid = win32process.GetWindowThreadProcessId(hwnd)
            if wnd_pid == pid:
                results.append(hwnd)

    win32gui.EnumWindows(_enum_cb, None)
    return results


def find_window(process_name: str, title_regex: str | None = None) -> WindowInfo | None:
    """按进程名（+标题正则）定位可见窗口"""
    if win32gui is None:
        return None

    compiled = re.compile(title_regex) if title_regex else None

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name != process_name.lower():
                continue
            for hwnd in _find_windows_by_pid(proc.info["pid"]):
                title = win32gui.GetWindowText(hwnd)
                if not title:
                    continue
                if compiled and not compiled.search(title):
                    continue
                rect = _get_window_rect(hwnd)
                return WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    pid=proc.info["pid"],
                    process_name=name,
                    rect=rect,
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _get_window_rect(hwnd: int) -> WindowRect | None:
    """取窗口屏幕坐标；窗口无效/最小化时返回 None"""
    if win32gui is None:
        return None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001
        return None
    width = max(0, right - left)
    height = max(0, bottom - top)
    if width <= 0 or height <= 0:
        return None
    return WindowRect(left=left, top=top, width=width, height=height)


def is_window_minimized(hwnd: int) -> bool:
    """判断窗口是否处于最小化状态（Win32 IsIconic）。

    PoC B2 保留项：Trae 最小化必崩 WGC（WorkBuddy 偶崩），orchestrator
    据此在最小化时主动停 WGC（避免原生崩溃）→ DXGI 兜底 → 恢复后重建。
    """
    if win32gui is None:
        return False
    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:  # noqa: BLE001
        return False
