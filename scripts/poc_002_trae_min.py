"""Trae 专项：最小化/恢复行为隔离验证（防全场景崩溃丢数据）

分阶段执行，每阶段 flush 输出：
  A. 可见态采集若干帧（确认出帧基线）
  B. 主动最小化 6s：统计最小化期间出帧（WGC 是否停帧/崩溃）
  C. 恢复：统计恢复后首帧时间（≤3s 标准）

用法: python scripts/poc_002_trae_min.py [--frames 10]
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
faulthandler.enable()

from poc_002_capture_core import (  # noqa: E402
    CaptureRun,
    find_hwnd_by_title,
    is_minimized,
    minimize_window,
    restore_window,
)

TITLE = "TRAE Work CN [管理员]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=10)
    args = ap.parse_args()

    hwnd = find_hwnd_by_title(TITLE)
    print(f"hwnd={hwnd} iconic_before={is_minimized(hwnd)}", flush=True)
    if hwnd is None:
        print("WINDOW_NOT_FOUND", flush=True)
        return 1

    restore_window(hwnd)
    time.sleep(1.0)
    print(f"iconic_after_restore={is_minimized(hwnd)}", flush=True)

    run = CaptureRun(TITLE, Path("tmp/poc002_b2/trae_min"))
    run.start()
    print(f"[A] 采集可见态 {args.frames} 帧...", flush=True)
    t_end = time.time() + 40
    while len(run.frames) < args.frames and time.time() < t_end:
        if is_minimized(hwnd):
            print("  [A] 窗口自动最小化，恢复", flush=True)
            restore_window(hwnd)
        time.sleep(0.2)
    stA = run.stats()
    print(f"[A结果] frames={stA['frames']} empty={stA['empty']} black={stA['black']} "
          f"valid={stA.get('valid_rate')}% dims={stA.get('dims')} "
          f"consistent={stA.get('resolution_consistent')}", flush=True)

    # B: 最小化 6s
    n_before = len(run.frames)
    print("[B] 主动最小化 6s...", flush=True)
    minimize_window(hwnd)
    time.sleep(6.0)
    n_during = len(run.frames)
    seg = run.frames[n_before:n_during]
    stB = {
        "minimize_frames": len(seg),
        "empty": sum(1 for f in seg if f["empty"]),
        "black": sum(1 for f in seg if f["black"]),
        "secs": 6.0,
    }
    print(f"[B结果] 最小化6s出帧={stB['minimize_frames']} 空={stB['empty']} 黑={stB['black']}",
          flush=True)

    # C: 恢复续帧
    n_before_restore = len(run.frames)
    t_restore = time.time()
    restore_window(hwnd)
    first_frame_at = None
    t_end = time.time() + 8
    while time.time() < t_end:
        if len(run.frames) > n_before_restore:
            first_frame_at = time.time() - t_restore
            break
        time.sleep(0.05)
    stC = {"restore_first_frame_s": round(first_frame_at, 2) if first_frame_at else None}
    print(f"[C结果] 恢复后首帧={stC['restore_first_frame_s']}s (<=3s)", flush=True)

    print("\n=== JSON ===", flush=True)
    print(json.dumps({"phaseA": stA, "phaseB": stB, "phaseC": stC,
                      "timeline": run.timeline[-15:]}, ensure_ascii=False, indent=2,
                     default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
