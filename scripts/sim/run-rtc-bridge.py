"""模拟启动器 · 第 1 步：在本机拉起 rtc_bridge（读 .env，兑付指向云端控制面）。

为什么用 Python 启动而不是 shell：
`.env` 里的服务凭证/断言是敏感值；由 Python 读入并放进子进程 env，可以避免它们出现在
shell 历史、命令行参数或 `ps` 输出里。`backend/rtc_bridge/config.py` 只读 `os.environ`，
所以必须在这里把 `.env` 装进环境。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "outputs" / "deploy-backup-20260911" / "local-rtc-bridge.log"


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def main() -> int:
    env = dict(os.environ)
    dot = load_env()
    env.update(dot)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT / "backend")
    LOG.parent.mkdir(parents=True, exist_ok=True)

    with LOG.open("wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "rtc_bridge.main"],
            cwd=str(ROOT / "backend"),
            env=env, stdout=fh, stderr=subprocess.STDOUT,
        )
    print(f"rtc_bridge pid={proc.pid} log={LOG}")
    print("control_plane =", dot.get("RTC_BRIDGE_CONTROL_PLANE_BASE_URL"))
    print("engine        =", dot.get("VOICE_ENGINE"), "| sdkappid =", dot.get("TRTC_SDKAPPID"))
    # 保持父进程存活：本进程作为后台任务托管时，它退出会连带收割子进程。
    print("holding... (kill this task to stop rtc_bridge)")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
