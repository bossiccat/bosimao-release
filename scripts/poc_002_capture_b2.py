"""PoC-B2 任务2：WGC 三窗口捕获综合实测（60 帧 + 场景覆盖）

覆盖：
  阶段A 可见态 60 帧：帧间隔/空帧/黑帧/分辨率一致性/单帧耗时
  阶段B 主动最小化 6s：WGC 是否停帧/空帧/黑帧
  阶段C 恢复续帧：恢复后首帧时间（3s 标准）
  阶段D 部分遮挡 20 帧：遮挡后是否仍出帧

用法: python scripts/poc_002_capture_b2.py --window "Hermes" [--frames 60]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from poc_002_capture_core import (
    CaptureRun,
    cover_window,
    find_hwnd_by_title,
    is_minimized,
    minimize_window,
    restore_window,
    uncover_window,
)

OUT_ROOT = Path("tmp/poc002_b2")


def run_scenario(title: str, frames: int, out_root: Path) -> dict:
    """执行全场景，返回汇总结果"""
    hwnd = find_hwnd_by_title(title)
    print(f"\n===== [{title}] hwnd={hwnd} =====", flush=True)
    if hwnd is None:
        print(f"  [错误] 未找到可见窗口 title={title!r}", flush=True)
        return {"title": title, "error": "window not found"}

    run = CaptureRun(title, out_root / title.replace(" ", "_"))
    # 先恢复窗口再启动捕获（最小化时 WindowsCapture.start 会阻塞不出帧）
    restore_window(hwnd)
    time.sleep(1.0)
    run.start()
    print(f"  [阶段A] 可见态采样 {frames} 帧...", flush=True)
    auto_min_count = 0
    t_end = time.time() + 45
    while len(run.frames) < frames and time.time() < t_end:
        if is_minimized(hwnd):
            auto_min_count += 1
            run._log("auto-minimized during visible, restoring")
            restore_window(hwnd)
        time.sleep(0.2)
    if len(run.frames) < frames:
        print(f"  [阶段A] 未达 {frames} 帧，当前 {len(run.frames)}", flush=True)
    stA = run.stats()
    stA["auto_min_count"] = auto_min_count
    print(f"  [阶段A结果] 帧数={stA['frames']} 空帧={stA['empty']} 黑帧={stA['black']} "
          f"有效={stA['valid_rate']:.1f}% 分辨率={stA['dims']} 一致={stA['resolution_consistent']}",
          flush=True)
    print(f"    帧间隔(ms): avg={stA['interval_avg_ms']} p95={stA['interval_p95_ms']} "
          f"max={stA['interval_max_ms']}", flush=True)
    print(f"    单帧 save avg={stA['save_avg_ms']}ms max={stA['save_max_ms']}ms | "
          f"downsample avg={stA['downsample_avg_ms']}ms | "
          f"合计 avg={stA['single_frame_total_avg_ms']}ms (标准<=200ms)", flush=True)

    # 阶段B：最小化
    n_before = len(run.frames)
    print(f"  [阶段B] 最小化 6s 观察...", flush=True)
    minimize_window(hwnd)
    t0_min = time.time()
    time.sleep(6.0)
    n_during = len(run.frames)
    min_frames = n_during - n_before
    min_frames_detail = run.frames[n_before:n_during]
    stB = {
        "minimize_frames": min_frames,
        "minimize_empty": sum(1 for f in min_frames_detail if f["empty"]),
        "minimize_black": sum(1 for f in min_frames_detail if f["black"]),
        "minimize_secs": round(time.time() - t0_min, 1),
    }
    print(f"  [阶段B结果] 最小化期间 {stB['minimize_secs']}s 出帧 {stB['minimize_frames']} "
          f"(空 {stB['minimize_empty']} / 黑 {stB['minimize_black']})", flush=True)

    # 阶段C：恢复续帧
    print(f"  [阶段C] 恢复窗口，等待续帧...", flush=True)
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
    print(f"  [阶段C结果] 恢复后首帧 {stC['restore_first_frame_s']}s (标准<=3s)", flush=True)

    # 阶段D：部分遮挡（用 explorer 的 Program Manager 覆盖一半）
    cover_hwnd = None
    try:
        import win32gui
        cover_hwnd = win32gui.FindWindow("Progman", "Program Manager")
    except Exception:
        cover_hwnd = None
    if cover_hwnd:
        print(f"  [阶段D] 遮挡 20 帧...", flush=True)
        n_before_cover = len(run.frames)
        try:
            cover_window(cover_hwnd, hwnd)
            t_end = time.time() + 15
            while len(run.frames) < n_before_cover + 20 and time.time() < t_end:
                if is_minimized(hwnd):
                    restore_window(hwnd)
                time.sleep(0.1)
        finally:
            uncover_window(cover_hwnd)
        cover_frames = run.frames[n_before_cover:]
        stD = {
            "covered_frames": len(cover_frames),
            "covered_empty": sum(1 for f in cover_frames if f["empty"]),
            "covered_black": sum(1 for f in cover_frames if f["black"]),
        }
        print(f"  [阶段D结果] 遮挡期出帧 {stD['covered_frames']} "
              f"(空 {stD['covered_empty']} / 黑 {stD['covered_black']})", flush=True)
    else:
        stD = {"covered_frames": 0, "note": "no cover window"}
        print(f"  [阶段D] 无 Progman 窗口，跳过遮挡", flush=True)

    run.stop()
    return {
        "title": title,
        "hwnd": hwnd,
        "phaseA": stA,
        "phaseB": stB,
        "phaseC": stC,
        "phaseD": stD,
        "timeline": run.timeline[-20:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True, help="窗口标题")
    ap.add_argument("--frames", type=int, default=60)
    args = ap.parse_args()
    res = run_scenario(args.window, args.frames, OUT_ROOT)
    print("\n=== JSON ===")
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
