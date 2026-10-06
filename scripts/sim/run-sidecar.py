"""模拟启动器 · 第 2 步：拉起 sidecar，控制面指向云端。

关键点
------
1. `--sign-url` 必须显式传云端控制面：`sidecar/config.js` 的默认值是 `https://127.0.0.1:8000`，
   不传就会去连本机 8000（那里没有控制面）。这是上一轮在云端模拟器上踩过的同一个坑。
2. `VOICE_SIDECAR_CREDENTIAL` 只从**进程环境**读（`config.js` 用的是 `process.env`，
   不是它自己 loadEnv 出来的对象），必须在这里注入。
3. 注入 `JAX_SIDECAR_LOG_DIR`：无头/窗口化 Electron 的渲染进程 stdout 不可靠，
   日志写文件（`logger.js`）。指到 outputs 下才能回读。
4. `SIM_SIDECAR_ROLE`（缺省 'sidecar'，行为与历史逐字节一致）：
   - 'sidecar'：旧证据路径（check1/check2）；
   - 'desktop'：check3 desktop 变体——argv 换成 `--role=desktop --device=<id>`
     （**不带** `--bridge-url`：desktop 拓扑没有本地桥），env 注入
     `VOICE_DESKTOP_DEVICE_CREDENTIAL`。设备 provisioning 由编排器
     （run-sim-e2e.py）完成，这里只消费 SIM_DESKTOP_DEVICE_ID /
     SIM_DESKTOP_DEVICE_CREDENTIAL，缺任一直接 fail-fast。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecar"
# 默认仍是旧证据目录；复跑验收时设 SIM_OUT_DIR 指向新目录，避免覆盖上一轮证据。
# ⚠️ 必须 .resolve()：本进程把 LOGDIR 传给 electron 子进程，而其 cwd=sidecar/；
# 相对路径会被 logger.js/main.js 的 path.resolve 解析到 sidecar/ 下 ⇒ 日志分裂、
# 编排器扫不到（2026-10-06 实锤，见 run-sim-e2e.py 同款注释）。
OUT_DIR = Path(os.environ.get("SIM_OUT_DIR")
               or (ROOT / "outputs" / "deploy-backup-20260911")).resolve()
LOGDIR = OUT_DIR / "sidecar-logs"
OUT = OUT_DIR / "local-sidecar.out.log"


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


def resolve_sim_role(environ: Mapping[str, str]) -> str:
    """SIM_SIDECAR_ROLE → 'sidecar' | 'desktop'。缺省与非法值都走显式分支。"""
    role = (environ.get("SIM_SIDECAR_ROLE") or "sidecar").strip().lower()
    if role not in ("sidecar", "desktop"):
        raise SystemExit(
            f"FATAL: SIM_SIDECAR_ROLE 仅支持 sidecar|desktop，实得 {role!r}"
        )
    return role


def build_launch(
    dot: Mapping[str, str],
    base_env: Mapping[str, str],
    *,
    role: str,
    electron: str,
    desktop_device_id: str = "",
    desktop_credential: str = "",
) -> tuple[list[str], dict[str, str]]:
    """构造 (argv, env)。role='sidecar' 时与历史行为逐字节一致。"""
    api = dot["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"].rstrip("/")
    env = dict(base_env)
    if role == "desktop":
        if not desktop_device_id or not desktop_credential:
            raise SystemExit(
                "FATAL: SIM_SIDECAR_ROLE=desktop 需要 SIM_DESKTOP_DEVICE_ID 与 "
                "SIM_DESKTOP_DEVICE_CREDENTIAL（由 run-sim-e2e.py 的 desktop 腿 provision）"
            )
        argv = [
            electron, ".", "--role=desktop",
            f"--device={desktop_device_id}",
            f"--sign-url={api}",
        ]
        env["VOICE_DESKTOP_DEVICE_CREDENTIAL"] = desktop_credential
    else:
        argv = [
            electron, ".", "--role=sidecar",
            "--bridge-url=ws://127.0.0.1:19092",
            f"--sign-url={api}",
        ]
    return argv, env


def main() -> int:
    dot = load_env()
    role = resolve_sim_role(os.environ)
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

    argv, env = build_launch(
        dot, env, role=role, electron=electron_bin(),
        desktop_device_id=os.environ.get("SIM_DESKTOP_DEVICE_ID", ""),
        desktop_credential=os.environ.get("SIM_DESKTOP_DEVICE_CREDENTIAL", ""),
    )
    print("electron =", argv[0])
    print("role =", role)
    print("sign_url =", dot["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"].rstrip("/"))
    print("sidecar credential present =", bool(dot.get("VOICE_SIDECAR_CREDENTIAL")))
    if role == "desktop":
        print("desktop credential present =", bool(os.environ.get("SIM_DESKTOP_DEVICE_CREDENTIAL")))
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
