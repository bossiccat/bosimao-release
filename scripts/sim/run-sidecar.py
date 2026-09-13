"""模拟启动器 · 第 2 步：拉起 sidecar（--role=sidecar），控制面指向云端。

关键点
------
1. `--sign-url` 必须显式传云端控制面：`sidecar/config.js` 的默认值是 `https://127.0.0.1:8000`，
   不传就会去连本机 8000（那里没有控制面）。这是上一轮在云端模拟器上踩过的同一个坑。
2. `VOICE_SIDECAR_CREDENTIAL` 只从**进程环境**读（`config.js` 用的是 `process.env`，
   不是它自己 loadEnv 出来的对象），必须在这里注入。
3. 注入 `JAX_SIDECAR_LOG_DIR`：无头/窗口化 Electron 的渲染进程 stdout 不可靠，
   日志写文件（`logger.js`）。指到 outputs 下才能回读。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecar"
LOGDIR = ROOT / "outputs" / "deploy-backup-20260911" / "sidecar-logs"
OUT = ROOT / "outputs" / "deploy-backup-20260911" / "local-sidecar.out.log"


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


def electron_bin() -> str:
    for cand in (SIDECAR / "node_modules" / "electron" / "dist" / "electron.exe",
                 SIDECAR / "node_modules" / ".bin" / "electron.cmd"):
        if cand.is_file():
            return str(cand)
    raise SystemExit("FATAL: 找不到 electron 可执行文件")


def main() -> int:
    dot = load_env()
    env = dict(os.environ)
    env.update(dot)
    env["JAX_SIDECAR_LOG_DIR"] = str(LOGDIR)
    # 必须清掉：本机环境带着 ELECTRON_RUN_AS_NODE=1（宿主自身是 Electron 应用时常见），
    # 会让 electron.exe 退化成纯 Node，require('electron') 只返回模块路径 →
    # `ipcMain` 为 undefined → main.js 第 26 行 TypeError 立即退出。
    env.pop("ELECTRON_RUN_AS_NODE", None)
    # main.js 的 fail-closed 守卫：没有 NODE_EXTRA_CA_CERTS 就拒绝启动（TLS pinning，ADR-020 A1）。
    # 控制面已迁到公网域名，CA 用仓库里的公共 CA bundle（与 rtc_bridge 兑付用的是同一份）。
    ca = dot.get("RTC_BRIDGE_CONTROL_PLANE_CA_FILE") or str(ROOT / "certs" / "cloud-public-ca-bundle.pem")
    env["NODE_EXTRA_CA_CERTS"] = ca
    print("NODE_EXTRA_CA_CERTS =", ca, "| exists =", Path(ca).is_file())
    LOGDIR.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    api = dot["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"].rstrip("/")
    argv = [
        electron_bin(), ".", "--role=sidecar",
        "--bridge-url=ws://127.0.0.1:19092",
        f"--sign-url={api}",
    ]
    print("electron =", argv[0])
    print("sign_url =", api)
    print("sidecar credential present =", bool(dot.get("VOICE_SIDECAR_CREDENTIAL")))
    with OUT.open("wb") as fh:
        proc = subprocess.Popen(argv, cwd=str(SIDECAR), env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    print(f"sidecar pid={proc.pid} stdout_log={OUT} renderer_log_dir={LOGDIR}")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
