"""Trae 专项诊断：WGC 捕获稳定性 + faulthandler 崩溃捕获

用法: python scripts/poc_002_trae_diag.py [--frames N] [--timeout S]
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

faulthandler.enable()

from poc_002_capture_core import (  # noqa: E402
    CaptureRun,
    find_hwnd_by_title,
    is_minimized,
    restore_window,
)

TITLE = "TRAE Work CN [管理员]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=50.0)
    ap.add_argument("--out", default="tmp/poc002_b2/trae_diag")
    args = ap.parse_args()

    hwnd = find_hwnd_by_title(TITLE)
    print(f"hwnd={hwnd} iconic_before={is_minimized(hwnd)}", flush=True)
    if hwnd is None:
        print("WINDOW_NOT_FOUND", flush=True)
        return 1

    restore_window(hwnd)
    time.sleep(1.0)
    print(f"iconic_after={is_minimized(hwnd)}", flush=True)

    run = CaptureRun(TITLE, Path(args.out))
    run.start()
    t0 = time.time()
    n_auto_restore = 0
    while len(run.frames) < args.frames and time.time() - t0 < args.timeout:
        if is_minimized(hwnd):
            n_auto_restore += 1
            restore_window(hwnd)
            run._log(f"auto-restored #{n_auto_restore}")
        time.sleep(0.1)

    st = run.stats()
    st["auto_restore_count"] = n_auto_restore
    st["elapsed_s"] = round(time.time() - t0, 1)
    print("\n=== RESULT ===", flush=True)
    print(json.dumps(st, ensure_ascii=False, indent=2), flush=True)
    print("=== TIMELINE ===", flush=True)
    for t, ev in run.timeline:
        print(f"  {t:6.2f}s {ev}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
