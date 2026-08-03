"""PoC-002: WGC 三窗口捕获验证（风险②）
对 Codex/Trae/Hermes 各建捕获会话采样 60 帧，统计帧间隔 / 空帧比例 / 分辨率一致性。
用法:
  python scripts/poc_002_capture.py [--seconds 60] [--frames 60] [--window "trae"]
  python scripts/poc_002_capture.py --help
退出码: 0=全部 PASS, 1=参数非法或无窗口匹配, 2=运行异常

⚠️ 首次运行会弹系统选择器要求授权（点"允许"），对每个窗口授权一次
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

try:
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
except ImportError as e:  # pragma: no cover
    print(f"[错误] 依赖缺失，请先运行 setup_env.ps1: {e}", file=sys.stderr)
    sys.exit(2)

TARGETS = {
    "codex": r"(?i)codex",
    "trae": r"(?i)trae",
    "hermes": r"(?i)hermes",
}


def run_window(window_title: str, seconds: int, frames: int, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {
        "frames": 0, "empty": 0, "dims": set(), "intervals_ms": [],
        "closed": False, "start": time.time(), "last_ts": None,
    }

    capture = WindowsCapture(window_name=window_title, cursor_capture=None, draw_border=None)

    @capture.event
    def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
        now = time.time()
        if stats["last_ts"] is not None:
            stats["intervals_ms"].append((now - stats["last_ts"]) * 1000)
        stats["last_ts"] = now
        stats["frames"] += 1
        w, h = frame.width, frame.height
        stats["dims"].add(f"{w}x{h}")
        if w <= 1 or h <= 1:
            stats["empty"] += 1
        if now - stats["start"] >= seconds or (frames and stats["frames"] >= frames):
            capture_control.stop()

    @capture.event
    def on_closed(capture_control: InternalCaptureControl):
        stats["closed"] = True

    capture.start()  # 阻塞至 stop
    elapsed = time.time() - stats["start"]
    valid = stats["frames"] - stats["empty"]
    stats["valid_rate"] = valid / stats["frames"] * 100 if stats["frames"] else 0
    stats["fps"] = stats["frames"] / elapsed if elapsed else 0
    iv = stats["intervals_ms"]
    stats["interval_avg_ms"] = round(statistics.mean(iv), 1) if iv else None
    stats["interval_min_ms"] = round(min(iv), 1) if iv else None
    stats["interval_max_ms"] = round(max(iv), 1) if iv else None
    stats["interval_p95_ms"] = round(sorted(iv)[int(len(iv) * 0.95) - 1], 1) if iv else None
    stats["resolution_consistent"] = len(stats["dims"]) == 1
    stats["elapsed_s"] = round(elapsed, 1)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="PoC-002 WGC 三窗口捕获验证")
    ap.add_argument("--seconds", type=int, default=60, help="每个窗口采样时长(秒)")
    ap.add_argument("--frames", type=int, default=60, help="每个窗口采样帧数上限(0=不限，仅按时长)")
    ap.add_argument("--window", default=None, help="指定窗口标题（默认遍历三个）")
    args = ap.parse_args()

    if args.seconds <= 0:
        print("[错误] --seconds 必须为正整数", file=sys.stderr)
        return 1
    if args.frames < 0:
        print("[错误] --frames 不能为负数", file=sys.stderr)
        return 1

    targets = [args.window] if args.window else list(TARGETS)
    out_root = Path("tmp/poc002")
    overall_ok = True

    for title in targets:
        print(f"\n=== 捕获窗口: {title}（{args.seconds}s / {args.frames or '不限'}帧）===")
        try:
            stats = run_window(title, args.seconds, args.frames, out_root / title.replace(" ", "_"))
            print(f"  帧数={stats['frames']} 空帧={stats['empty']} 分辨率={sorted(stats['dims'])}")
            print(f"  有效帧率={stats['valid_rate']:.1f}%  fps={stats['fps']:.2f}  耗时={stats['elapsed_s']}s")
            print(f"  帧间隔(ms): avg={stats['interval_avg_ms']} min={stats['interval_min_ms']} "
                  f"max={stats['interval_max_ms']} p95={stats['interval_p95_ms']}")
            print(f"  分辨率一致={stats['resolution_consistent']}")
            ok = stats["valid_rate"] >= 95 and stats["frames"] >= 10
            overall_ok = overall_ok and ok
            print(f"  判定: {'PASS' if ok else 'FAIL (需 >=95% 有效帧率)'}")
            print("  [场景提示] 最小化后恢复：正常应在 3s 内自动续帧（DXGI 兜底或 WGC 自恢复）。"
                  "如未恢复请记录为 FAIL 并走 POC-002 备用方案。")
        except Exception as e:  # noqa: BLE001
            overall_ok = False
            print(f"  [错误] 捕获失败（窗口不存在或未授权）: {e}")
            print("  请确认目标窗口已打开，且已在系统选择器中点'允许'授权。")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
