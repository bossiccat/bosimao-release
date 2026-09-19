#!/usr/bin/env python
"""Uplink capture level sampler with time alignment, plus a complete session trace.

Purpose
-------
Two questions, one run each:
  1. Does the in-session capture path receive audio, or only silence?
  2. What does the session lifecycle actually do -- enter, stay, get destroyed?

Why the trace is streamed instead of dumped
-------------------------------------------
The first version called `logcat -d` at the end of the window. That only returns
whatever survived the log ring buffer, and the app's own 500 ms level line plus
the system's VRI frame-rate spam evict the very lines that matter: a 90 s run left
344 buffered lines and, after removing noise, three. `EnterRoom` and the state
machine's `start accepted` were already gone, which is why an earlier session
looked like it had no lifecycle events at all.

So the log is now streamed to a file from before the tap until the window closes.
Nothing rotates away, and the analysis can be redone later from the saved file.

Why the level buckets matter
----------------------------
Earlier sampling was never aligned with whether anyone was speaking, so "always
zero" and "nobody spoke into the window" were indistinguishable -- that confusion
invalidated three conclusions on 2026-09-16. Bucketing on *device* timestamps
removes the ambiguity structurally. On that run the in-session path read up to
raw=1029 in bursts aligned with speech, so the capture path is fine; the real
defect was a session whose TRTC channel had been destroyed while the state
machine still reported IN_ROOM and refused new starts with `conflict`.

Reading the output
------------------
  speaking buckets still 0             -> capture path really delivers zeros
  speaking buckets >0, quiet buckets 0 -> capture works; look at session/downlink
  no RtcCustomAudio lines at all       -> session/capture never started
  DestroyChannel with no following exit -> phantom session: the state machine
                                          still believes it is IN_ROOM

The device address is a one-off value (wireless debugging rotates its port), so it
only ever comes from the command line -- never from a default.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LINE_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.\d+\s+\S+/(\S+?)\s*\(\s*\d+\):\s*lvl raw=(\d+)"
)
SAMPLE_TAGS = ("RtcCustomAudio", "MicRecorder")
NOISE = ("VRI[", "Choreographer", "ProfileInstaller", "Compat change",
         "io_working_status_printer", "LiteavBaseSystemInfo", "ntp_time_manager",
         "thread_watchdog", "http_client_android", "lvl raw=")
LIFECYCLE_KEYS = ("VoiceSessionCoord", "VoiceService", "EnterRoom", "DestroyChannel",
                  "exitRoom", "first frame", "Read first frame", "audioStatus",
                  "BargeIn", "interrupt", "sign ", "sign_", " E/", " W/",
                  "audioRoute", "AudioDevice", "mute")


def run(argv: list[str], timeout: int = 240) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return (proc.stdout + proc.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sample uplink levels and trace the session.")
    ap.add_argument("--adb", default=r"tmp\task6-tools\platform-tools\adb.exe")
    ap.add_argument("--device", required=True,
                    help="host:port from the wireless debugging screen (required: it rotates)")
    ap.add_argument("--pkg", default="com.jax.voice")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--bucket", type=int, default=10)
    args = ap.parse_args()

    def sh(cmd: str, timeout: int = 240) -> str:
        return run([args.adb, "shell", cmd], timeout)

    print(run([args.adb, "connect", args.device])[:120])
    listing = [ln for ln in run([args.adb, "devices"]).splitlines() if args.device in ln]
    if not listing or "device" not in listing[0]:
        print(f"device not ready: {listing or 'no entry'} -- the wireless debugging port rotates.")
        return 2
    print("device:", listing[0])

    sh(f"am force-stop {args.pkg}")
    time.sleep(2)
    sh("svc power stayon true")
    sh(f"am start -n {args.pkg}/.MainActivity")
    time.sleep(6)

    pid = sh(f"pidof {args.pkg}").split()
    if not pid:
        print("app not running")
        return 3
    pid = pid[0]

    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "session-trace.log"

    # Stream first, tap second: the lifecycle lines are emitted within
    # milliseconds of the tap and a later `logcat -d` would already have lost them.
    with log_path.open("w", encoding="utf-8", errors="replace") as sink:
        streamer = subprocess.Popen([args.adb, "logcat", "-v", "time", "--pid", pid],
                                    stdout=sink, stderr=subprocess.STDOUT)
        time.sleep(1.5)

        sh("uiautomator dump /sdcard/_upl.xml >/dev/null 2>&1")
        ui = sh("cat /sdcard/_upl.xml")
        m = re.search(r'text="(立即对话|停止监听)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', ui)
        if m:
            cx = (int(m.group(2)) + int(m.group(4))) // 2
            cy = (int(m.group(3)) + int(m.group(5))) // 2
            print(f"main button = {m.group(1)} at ({cx},{cy})")
            if m.group(1) == "立即对话":
                sh(f"input tap {cx} {cy}")
        else:
            print("main button not found; tracing anyway")

        t0 = sh("date +%H:%M:%S")
        print(f"PID={pid} window opens at device time {t0} ({args.seconds}s); "
              "speaking is optional this run", flush=True)
        time.sleep(args.seconds)
        streamer.terminate()
        try:
            streamer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            streamer.kill()

    out = log_path.read_text(encoding="utf-8", errors="replace")
    print(f"traced {len(out.splitlines())} lines -> {log_path}")

    start = datetime.strptime(t0, "%H:%M:%S")
    series: dict[str, list[tuple[float, int]]] = {k: [] for k in SAMPLE_TAGS}
    for line in out.splitlines():
        hit = LINE_RE.search(line)
        if not hit:
            continue
        hh, mm, ss, tag, val = hit.groups()
        try:
            ts = datetime.strptime(f"{hh}:{mm}:{ss}", "%H:%M:%S")
        except ValueError:
            continue
        off = (ts - start).total_seconds()
        if off < -20 or off > args.seconds + 40:
            continue
        for known in SAMPLE_TAGS:
            if known in tag:
                series[known].append((off, int(val)))
                break

    for key in SAMPLE_TAGS:
        vals = series[key]
        print()
        print(f"== {key} == {len(vals)} readings")
        if not vals:
            print("   (none -- this path was not running)")
            continue
        print("   secs      n  nonzero   mean   max   nonzero samples")
        for b in range(max(1, (args.seconds + 2 * args.bucket) // args.bucket)):
            lo, hi = b * args.bucket, (b + 1) * args.bucket
            win = [v for off, v in vals if lo <= off < hi]
            if not win:
                continue
            nz = [v for v in win if v > 0]
            print(f"   {lo:3d}-{hi:3d} {len(win):5d} {len(nz):8d} "
                  f"{sum(win) / len(win):6.1f} {max(win):5d}   {nz[:8]}")

    print()
    print("== session lifecycle (complete, in order) ==")
    shown = 0
    for line in out.splitlines():
        if any(n in line for n in NOISE):
            continue
        if any(k in line for k in LIFECYCLE_KEYS):
            print("  ", line[:190])
            shown += 1
            if shown >= 80:
                print("   ... (truncated at 80; full file saved)")
                break
    if not shown:
        print("   (no lifecycle lines at all -- the app logged nothing beyond levels)")

    rtc = [v for _, v in series["RtcCustomAudio"]]
    print()
    print("== verdict ==")
    if not rtc:
        print("no in-session level lines -> session never started, or capture never began")
    elif any(v > 0 for v in rtc):
        print(f"in-session capture read non-zero (max={max(rtc)}) -> capture receives audio; "
              "check the session/downlink side, not capture")
    else:
        print("in-session capture read 0 for the whole window -> capture delivers silence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
