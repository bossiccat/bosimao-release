"""PoC-B2 任务1：窗口匹配探针 — 验证 find_window(process_name+title_regex)

对 4 个监控目标分别：
1) 调用 backend.app.capture.window_finder.find_window 验证定位结果
2) 枚举该进程组全部可见窗口，判断"第一个匹配窗口是否主窗口"
3) 记录每目标 hwnd / 标题 / rect
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import psutil  # noqa: E402

try:
    import win32gui  # noqa: E402
    import win32process  # noqa: E402
    import win32con  # noqa: E402
except ImportError:
    win32gui = win32process = win32con = None

from app.capture.window_finder import find_window, WindowRect  # noqa: E402

TARGETS = [
    {"app_id": "codex", "process_name": "codex.exe", "title_regex": r"(?i)codex"},
    {"app_id": "trae", "process_name": "TRAE SOLO CN.exe", "title_regex": r"(?i)trae"},
    {"app_id": "hermes", "process_name": "hermes.exe", "title_regex": r"(?i)hermes"},
    {"app_id": "workbuddy", "process_name": "WorkBuddy.exe", "title_regex": r"(?i)workbuddy"},
]


def all_visible_windows_for(process_name: str) -> list[dict]:
    """枚举进程名下所有可见顶层窗口（含标题/rect/pid）"""
    if win32gui is None:
        return []
    out: list[dict] = []
    pids = {
        p.info["pid"]
        for p in psutil.process_iter(["pid", "name"])
        if (p.info["name"] or "").lower() == process_name.lower()
    }
    if not pids:
        return out

    def _cb(hwnd: int, _extra: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, wnd_pid = win32process.GetWindowThreadProcessId(hwnd)
        if wnd_pid not in pids:
            return
        title = win32gui.GetWindowText(hwnd)
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        # 窗口最小化时 GetWindowRect 仍可返回（-32000 特殊值）
        icon = ""
        try:
            if win32gui.IsIconic(hwnd):
                icon = "[MINIMIZED]"
        except Exception:
            pass
        out.append({
            "hwnd": hwnd, "pid": wnd_pid, "title": title, "rect": (l, t, r, b),
            "w": max(0, r - l), "h": max(0, b - t), "flag": icon,
        })

    win32gui.EnumWindows(_cb, None)
    out.sort(key=lambda x: (-(x["w"] * x["h"]), x["hwnd"]))  # 主窗口一般最大
    return out


def fmt_rect(rect: WindowRect | None) -> str:
    if rect is None:
        return "None"
    return f"({rect.left},{rect.top},{rect.width}x{rect.height})"


def main() -> int:
    print("=" * 78)
    print("Task 1: window matching probe — find_window(process_name, title_regex)")
    print("=" * 78)
    any_fail = False
    for t in TARGETS:
        print(f"\n--- [{t['app_id']}] process={t['process_name']!r} regex={t['title_regex']!r} ---")
        all_wins = all_visible_windows_for(t["process_name"])
        if not all_wins:
            print(f"  [未运行] 进程 '{t['process_name']}' 无任何可见窗口（进程可能未启动或名称不符）")
            any_fail = True
            continue
        print(f"  进程组可见窗口数: {len(all_wins)}")
        for i, w in enumerate(all_wins):
            mark = " <= 主窗口(最大)" if i == 0 else ""
            print(f"    [{i}] hwnd={w['hwnd']:>8} pid={w['pid']:>6} rect=({w['rect'][0]},{w['rect'][1]}"
                  f",{w['w']}x{w['h']}) {w['flag']} title={w['title']!r}{mark}")

        # find_window 结果
        win = find_window(t["process_name"], t["title_regex"])
        if win is None:
            print(f"  find_window => None  <-- FAIL")
            any_fail = True
            continue
        print(f"  find_window => hwnd={win.hwnd} pid={win.pid} title={win.title!r} "
              f"rect={fmt_rect(win.rect)}  <-- OK")
        # 是否命中主窗口（面积最大）
        biggest = all_wins[0]
        hit_main = win.hwnd == biggest["hwnd"]
        print(f"  命中主窗口(面积最大)? {'YES' if hit_main else 'NO'}")
        if not hit_main:
            any_fail = True
            print("  [警告] find_window 返回的不是最大窗口，需核对标题正则")
    print("\n" + "=" * 78)
    print("RESULT:", "PASS" if not any_fail else "FAIL")
    return 0 if not any_fail else 1


if __name__ == "__main__":
    sys.exit(main())
