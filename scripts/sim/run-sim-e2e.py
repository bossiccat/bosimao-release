"""端到端模拟：rtc_bridge -> sidecar -> phone（--role=phone），单进程编排。

为什么必须写在「一个进程」里
--------------------------
本平台 Bash 工具会在每次调用结束时**收割该次调用产生的全部子进程**。所以「起常驻进程」
和「用它做测量」必须落在同一条命令（同一个进程树）里；否则 rtc_bridge/sidecar 会在
两次工具调用之间被静默杀掉，phone 只能拿到假的 no_reply。

编排顺序
--------
1. 起 rtc_bridge（Popen 包装 scripts/sim/run-rtc-bridge.py），等它监听 127.0.0.1:19092
2. 起 sidecar（Popen 包装 scripts/sim/run-sidecar.py）
3. 等 sidecar **自身**启动完成（`[SIG] 意图轮询已启动`），超时 60s 就 dump 尾部并非 0 退出
4. 以子进程运行 scripts/sim/run-phone.py（约 45s），捕获完整 stdout
5. finally: 用 subprocess 调 `taskkill /F /T /PID` 杀净两棵进程树

⚠️ 第 3 步的判据必须是「sidecar 启动完成」，**不能**是「sidecar 已进房」——
   sidecar 是**被手机唤醒后**才连 bridge 并进房的（`[SIG] 意图轮询`每 2s 找唤醒信号）。
   等待 sidecar 先进房 = 死锁：手机不起 → sidecar 不进房 → 编排器等超时 → 手机永远不跑。
   （2026-09-13 实测：这一版编排器就是这样卡在 60s 超时的。）

注意：不要在 Git Bash 里直接写 `taskkill //PID`（MSYS 路径转换会破坏参数）；
用 subprocess 的**列表参数**形式。taskkill 输出是 GBK，解码用 errors='replace'。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "outputs" / "deploy-backup-20260911"
BRIDGE_LOG = D / "local-rtc-bridge.log"
SIDECAR_MAIN_LOG = D / "sidecar-logs" / "sidecar-sidecar.log"
SIDECAR_ALT_LOG = D / "sidecar-logs" / "sidecar-main-diag.log"
PHONE_OUT = D / "e2e-phone.out.txt"
SUMMARY = D / "e2e-summary.json"

PY = sys.executable
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\]")
# sidecar **自身**启动完成的标志。不能用「进房成功」——那要等手机唤醒（见文件头说明）。
SIDECAR_READY_MARKER = "[SIG] 意图轮询已启动"


def tail(path: Path, n: int = 40) -> list[str]:
    if not path.is_file():
        return [f"(missing: {path})"]
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def wait_port(port: int, timeout_s: float) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def log_line_fresh(line: str, start_epoch: float) -> bool:
    """行内 [..Z] 时间戳晚于 start_epoch 才算是本轮新行。"""
    m = TS_RE.match(line)
    if not m:
        return True  # 无时间戳的行无从判断，按新行处理
    try:
        ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return ts.timestamp() >= start_epoch - 10


def find_ready(start_epoch: float) -> tuple[bool, Path | None, list[str]]:
    for path in (SIDECAR_MAIN_LOG, SIDECAR_ALT_LOG):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            if SIDECAR_READY_MARKER in line and log_line_fresh(line, start_epoch):
                return True, path, lines[-3:]
    return False, None, []


def taskkill_tree(pid: int, name: str) -> str:
    try:
        p = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        out = (p.stdout or b"").decode("gbk", errors="replace") + \
              (p.stderr or b"").decode("gbk", errors="replace")
        return f"[{name} pid={pid}] rc={p.returncode} {out.strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"[{name} pid={pid}] taskkill failed: {type(exc).__name__}: {exc}"


def main() -> int:
    start = time.time()
    D.mkdir(parents=True, exist_ok=True)
    summary: dict = {"steps": {}, "start_epoch": start}
    bridge = sidecar = None

    try:
        # 1) rtc_bridge
        # JAX_DOWN_PCM_DUMP：把「节拍之后、WS 发送之前」的下行 PCM 落盘（session.py:514-523），
        # 这是桥侧"真的送出了多少帧"的唯一真值 —— 用来和手机侧收到的帧数逐级对账。
        bridge_env = dict(os.environ)
        bridge_env["JAX_DOWN_PCM_DUMP"] = str(D / "e2e-down")
        bridge = subprocess.Popen([PY, str(ROOT / "scripts" / "sim" / "run-rtc-bridge.py")],
                                  cwd=str(ROOT), env=bridge_env)
        summary["steps"]["bridge_pid"] = bridge.pid
        print(f"[1/4] rtc_bridge pid={bridge.pid} 等待 19092 ...", flush=True)
        if not wait_port(19092, 30):
            summary["steps"]["bridge_ready"] = False
            print("FATAL: rtc_bridge 30s 内未监听 19092。日志尾部：")
            print("\n".join(tail(BRIDGE_LOG, 40)))
            return 2
        summary["steps"]["bridge_ready"] = True
        print("[1/4] rtc_bridge 就绪", flush=True)

        # 2) sidecar
        sidecar = subprocess.Popen([PY, str(ROOT / "scripts" / "sim" / "run-sidecar.py")],
                                   cwd=str(ROOT))
        summary["steps"]["sidecar_pid"] = sidecar.pid
        print(f"[2/4] sidecar pid={sidecar.pid} 等待 sidecar 启动完成（≤60s）...", flush=True)

        # 3) 轮询 sidecar 启动完成（**不是**进房——见文件头说明）
        ready, log_path, ctx = False, None, []
        end = time.time() + 60
        while time.time() < end:
            ready, log_path, ctx = find_ready(start)
            if ready:
                break
            time.sleep(1.0)
        summary["steps"]["sidecar_booted"] = ready
        summary["steps"]["sidecar_booted_from"] = str(log_path) if log_path else None
        if not ready:
            print(f"FATAL: sidecar 60s 内没有出现「{SIDECAR_READY_MARKER}」。dump 尾部：")
            for lp in (SIDECAR_MAIN_LOG, SIDECAR_ALT_LOG, BRIDGE_LOG):
                print(f"----- {lp.name} -----")
                print("\n".join(tail(lp, 40)))
            return 3
        print(f"[3/4] sidecar 已启动（{log_path.name}）。上下文尾部：", flush=True)
        for ln in ctx:
            print("   ", ln, flush=True)

        # 4) phone
        print("[4/4] 运行 phone（约 45s）...", flush=True)
        t0 = time.time()
        with PHONE_OUT.open("w", encoding="utf-8") as fh:
            phone = subprocess.run([PY, str(ROOT / "scripts" / "sim" / "run-phone.py")],
                                   cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                                   timeout=240)
        summary["steps"]["phone_rc"] = phone.returncode
        summary["steps"]["phone_s"] = round(time.time() - t0, 1)
        print(f"[4/4] phone 退出 rc={phone.returncode}", flush=True)

        # 桥侧账目（必须在 kill 之前读）：/metrics 给出 down_frames / queue_drops 等，
        # 用来与手机侧"到达帧数"逐级对账 —— 判定音频是丢在我们这层还是模型本来就短。
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:19093/metrics", timeout=5) as resp:
                summary["bridge_metrics"] = json.loads(resp.read().decode("utf-8", "replace"))
            print("bridge /metrics =", json.dumps(summary["bridge_metrics"], ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            summary["bridge_metrics"] = None
            print("bridge /metrics 读取失败:", type(exc).__name__, str(exc)[:200])

        txt = PHONE_OUT.read_text(encoding="utf-8", errors="replace")
        print("========== phone stdout ==========")
        print(txt)
        m = re.search(r"METRICS:\s*(\{.*?\n\})", txt, re.S)
        if m:
            try:
                summary["metrics"] = json.loads(m.group(1))
            except json.JSONDecodeError:
                summary["metrics"] = None
        SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    except subprocess.TimeoutExpired:
        print("FATAL: phone 运行超时（240s）")
        return 4
    finally:
        kills = []
        if sidecar is not None:
            kills.append(taskkill_tree(sidecar.pid, "sidecar"))
        if bridge is not None:
            kills.append(taskkill_tree(bridge.pid, "bridge"))
        print("---------- cleanup ----------")
        for k in kills:
            print(k)
        SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
