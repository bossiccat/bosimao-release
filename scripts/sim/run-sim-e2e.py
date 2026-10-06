"""端到端模拟：rtc_bridge -> sidecar -> phone（--role=phone），单进程编排。

为什么必须写在「一个进程」里
--------------------------
本平台 Bash 工具会在每次调用结束时**收割该次调用产生的全部子进程**。所以「起常驻进程」
和「用它做测量」必须落在同一条命令（同一个进程树）里；否则 rtc_bridge/sidecar 会在
两次工具调用之间被静默杀掉，phone 只能拿到假的 no_reply。

编排顺序（缺省 SIM_CHECK3_MODE=''，与历史逐字节一致）
--------------------------------------------------
1. 起 rtc_bridge（Popen 包装 scripts/sim/run-rtc-bridge.py），等它监听 127.0.0.1:19092
2. 起 sidecar（Popen 包装 scripts/sim/run-sidecar.py）
3. 等 sidecar **自身**启动完成（`[SIG] 意图轮询已启动`），超时 60s 就 dump 尾部并非 0 退出
4. 以子进程运行 scripts/sim/run-phone.py（约 45s），捕获完整 stdout
5. finally: 用 subprocess 调 `taskkill /F /T /PID` 杀净两棵进程树

⚠️ 第 3 步的判据必须是「sidecar 启动完成」，**不能**是「sidecar 已进房」——
   sidecar 是**被手机唤醒后**才连 bridge 并进房的（`[SIG] 意图轮询`每 2s 找唤醒信号）。
   等待 sidecar 先进房 = 死锁：手机不起 → sidecar 不进房 → 编排器等超时 → 手机永远不跑。
   （2026-09-13 实测：这一版编排器就是这样卡在 60s 超时的。）

desktop 变体（SIM_CHECK3_MODE=desktop）：生产拓扑两腿验收
--------------------------------------------------------
旧 check3 的本地 `--role=sidecar` 与云端 CloudRun 常驻 sidecar 用**同一份**
VOICE_SIDECAR_CREDENTIAL 抢 intent，云端在内网占尽先机 → 本地永远抢不到，
这套对账是 dead-by-design。所以 desktop 模式改成两腿，各验各的语义：

  desktop 腿（自发起会话 + 云端汇合）：
    a) 用 owner 凭证 provision 一台 platform='windows' 的模拟桌面设备
       （cloudbridge/sim_provision.provision_sim_device，设备名 jax-sim-desktop）；
    b) run-sidecar.py 以 SIM_SIDECAR_ROLE=desktop 拉起 `--role=desktop`
       （凭证经 SIM_DESKTOP_DEVICE_CREDENTIAL → VOICE_DESKTOP_DEVICE_CREDENTIAL）；
    c) 等 `sidecar-desktop.log` 里先出现 `[DESKTOP] 进房成功`（自发起），再出现
       `[DESKTOP] 模型端已进房 userId=jax-pc-sidecar`（云端 sidecar 领取 intent
       并进**同一间房** = 生产汇合），超时 90s；不 spawn 本地 rtc_bridge
       （desktop 拓扑没有本地桥），也不读 /metrics；
    d) 汇合成立（或超时带诊断）后**先杀净 desktop 进程树**再跑 phone 腿——
       云端 sidecar 一实例一房，不腾出来 phone 的 intent 就无人可领。

  phone 腿：与缺省模式完全一致；此模式下 intent 由云端 sidecar 确定性领取
    （本地竞争者已被杀），媒体对账读数即生产路径读数。

summary JSON：desktop 模式记录 desktop_rendezvous（join_ok / cloud_peer_joined /
elapsed_s / log_tail）与 phone 的 metrics；没有 bridge_metrics（桥不存在）。
本脚本按惯例**不做** PASS/FAIL 断言——上游读 summary 自行判定。

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
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
# 默认仍是旧证据目录；复跑验收时设 SIM_OUT_DIR 指向新目录（本进程会把它透传
# 给 run-rtc-bridge/run-sidecar/run-phone——三者读同一个变量），避免覆盖上一轮证据。
# ⚠️ 必须 .resolve()：SIM_OUT_DIR 写相对路径时，run-sidecar.py 会把相对 LOGDIR
# 传给 electron（其 cwd=sidecar/），logger.js path.resolve 后日志落进
# sidecar/outputs/...，编排器在仓库根扫不到 ⇒ 测量假阴性（2026-10-06 实锤，
# 证据 outputs/check3-desktop-20261006/：汇合真实成立却被判超时）。
D = Path(os.environ.get("SIM_OUT_DIR")
         or (ROOT / "outputs" / "deploy-backup-20260911")).resolve()
BRIDGE_LOG = D / "local-rtc-bridge.log"
SIDECAR_MAIN_LOG = D / "sidecar-logs" / "sidecar-sidecar.log"
SIDECAR_ALT_LOG = D / "sidecar-logs" / "sidecar-main-diag.log"
SIDECAR_DESKTOP_LOG = D / "sidecar-logs" / "sidecar-desktop.log"
PHONE_OUT = D / "e2e-phone.out.txt"
SUMMARY = D / "e2e-summary.json"

PY = sys.executable
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\]")
# sidecar **自身**启动完成的标志。不能用「进房成功」——那要等手机唤醒（见文件头说明）。
SIDECAR_READY_MARKER = "[SIG] 意图轮询已启动"
# desktop 变体（logger.js 按 role 分文件 → sidecar-desktop.log；行形态见 desktop-loop.js）。
DESKTOP_JOIN_MARKER = "[DESKTOP] 进房成功"
DESKTOP_PEER_MARKER = "[DESKTOP] 模型端已进房 userId=jax-pc-sidecar"
DESKTOP_RENDEZVOUS_TIMEOUT_S = 90
DESKTOP_TEARDOWN_SETTLE_S = 5  # 杀净 desktop 后给云端一点处理离房的时间，再开 phone 腿


def resolve_check3_mode(environ) -> str:
    """SIM_CHECK3_MODE → ''（缺省=历史行为）| 'desktop'。其他值 fail-fast。"""
    mode = (environ.get("SIM_CHECK3_MODE") or "").strip().lower()
    if mode and mode != "desktop":
        raise SystemExit(f"FATAL: SIM_CHECK3_MODE 仅支持 desktop（缺省为历史模式），实得 {mode!r}")
    return mode


def build_plan(mode: str) -> dict:
    """模式 → 编排步骤表。只做决策，不碰子进程（契约测试就测这张表）。"""
    desktop = mode == "desktop"
    return {
        "spawn_bridge": not desktop,
        "read_bridge_metrics": not desktop,
        "desktop_leg": desktop,
        "ready_timeout_s": DESKTOP_RENDEZVOUS_TIMEOUT_S if desktop else 60,
    }


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
    for path in (SIDECAR_MAIN_LOG, SIDECAR_ALT_LOG, SIDECAR_DESKTOP_LOG):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            if SIDECAR_READY_MARKER in line and log_line_fresh(line, start_epoch):
                return True, path, lines[-3:]
    return False, None, []


def desktop_rendezvous_progress(
    lines: Iterable[str], start_epoch: float,
) -> tuple[bool, bool]:
    """扫描 desktop 渲染日志行 → (join_ok, cloud_peer_joined)。

    只认**本轮新行**（log_line_fresh），且远端 userId 必须恰是 jax-pc-sidecar
    （云端常驻 sidecar 的固定 userId，见 sidecar/config.js SIDECAR_USER_ID）。
    """
    join = peer = False
    for line in lines:
        if not log_line_fresh(line, start_epoch):
            continue
        if DESKTOP_JOIN_MARKER in line:
            join = True
        if DESKTOP_PEER_MARKER in line:
            peer = True
    return join, peer


def find_desktop_rendezvous(
    start_epoch: float,
) -> tuple[bool, bool, Path | None, list[str]]:
    for path in (SIDECAR_DESKTOP_LOG, SIDECAR_ALT_LOG, SIDECAR_MAIN_LOG):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        join, peer = desktop_rendezvous_progress(lines, start_epoch)
        if join:
            return join, peer, path, lines[-6:]
    return False, False, None, []


def taskkill_tree(pid: int, name: str) -> str:
    try:
        p = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        out = (p.stdout or b"").decode("gbk", errors="replace") + \
              (p.stderr or b"").decode("gbk", errors="replace")
        return f"[{name} pid={pid}] rc={p.returncode} {out.strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"[{name} pid={pid}] taskkill failed: {type(exc).__name__}: {exc}"


def run_desktop_leg(summary: dict, start: float) -> "tuple[subprocess.Popen, dict]":
    """desktop 腿：provision 模拟桌面设备 → 拉起 --role=desktop → 等生产汇合。

    返回（sidecar 进程句柄，汇合证据 dict）。调用方负责杀进程树。
    """
    # 延迟 import：缺省模式的 import 图与历史一致（run-phone.py 同款接线方式）。
    sys.path.insert(0, str(ROOT / "cloudbridge"))
    import sim_provision  # noqa: E402

    dot: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            dot[m.group(1)] = v
    api = dot["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"].rstrip("/")
    owner = dot.get("VOICE_OWNER_CREDENTIAL", "")
    if not owner:
        raise SystemExit("FATAL: .env 缺 VOICE_OWNER_CREDENTIAL，desktop 腿无法 provision")

    print("[desktop] provisioning 模拟桌面设备（platform=windows, jax-sim-desktop）...",
          flush=True)
    device = sim_provision.provision_sim_device(
        api, owner, device_name="jax-sim-desktop", platform="windows",
    )
    print(f"[desktop] device_id = {device.device_id}", flush=True)
    summary["steps"]["desktop_device_id"] = device.device_id

    env = dict(os.environ)
    env["SIM_OUT_DIR"] = str(D)
    env["SIM_SIDECAR_ROLE"] = "desktop"
    env["SIM_DESKTOP_DEVICE_ID"] = device.device_id
    env["SIM_DESKTOP_DEVICE_CREDENTIAL"] = device.credential_token
    sidecar = subprocess.Popen([PY, str(ROOT / "scripts" / "sim" / "run-sidecar.py")],
                               cwd=str(ROOT), env=env)
    summary["steps"]["desktop_sidecar_pid"] = sidecar.pid
    t0 = time.time()
    print(f"[desktop] pid={sidecar.pid} 等待生产汇合（进房→云端 jax-pc-sidecar 进同房，"
          f"≤{DESKTOP_RENDEZVOUS_TIMEOUT_S}s）...", flush=True)

    join = peer = False
    log_path: Path | None = None
    log_tail: list[str] = []
    while time.time() < t0 + DESKTOP_RENDEZVOUS_TIMEOUT_S:
        join, peer, log_path, log_tail = find_desktop_rendezvous(start)
        if join and peer:
            break
        time.sleep(1.0)
    evidence = {
        "join_ok": join,
        "cloud_peer_joined": peer,
        "elapsed_s": round(time.time() - t0, 1),
        "log_tail": log_tail or tail(SIDECAR_DESKTOP_LOG, 12),
    }
    label = "汇合成立" if (join and peer) else "汇合超时/失败"
    print(f"[desktop] {label}（join={join} cloud_peer={peer} "
          f"elapsed={evidence['elapsed_s']}s from="
          f"{log_path.name if log_path else 'n/a'}）", flush=True)
    for ln in log_tail:
        print("   ", ln, flush=True)
    return sidecar, evidence


def main() -> int:
    start = time.time()
    mode = resolve_check3_mode(os.environ)
    plan = build_plan(mode)
    D.mkdir(parents=True, exist_ok=True)
    summary: dict = {"steps": {}, "start_epoch": start}
    if mode:
        summary["check3_mode"] = mode
    bridge = sidecar = None

    try:
        if plan["desktop_leg"]:
            sidecar, desktop_ev = run_desktop_leg(summary, start)
            summary["desktop_rendezvous"] = desktop_ev
            # 一实例一房：杀净 desktop 进程树，把云端 sidecar 让给 phone 腿。
            print("---------- desktop teardown ----------", flush=True)
            print(taskkill_tree(sidecar.pid, "desktop-sidecar"), flush=True)
            sidecar = None
            time.sleep(DESKTOP_TEARDOWN_SETTLE_S)
        else:
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
            end = time.time() + plan["ready_timeout_s"]
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
        # edge-tts 在仓库 venv 的 Scripts/ 里（未激活 venv 直跑 python 时不在 PATH，
        # 2026-10-06 实跑实锤 WinError 2）。把**当前解释器所在目录**前置到 PATH：
        # 用 venv python 跑就能解析到 edge-tts.exe，系统 python 跑则维持原状。
        phone_env = dict(os.environ)
        py_dir = str(Path(sys.executable).resolve().parent)
        if py_dir not in phone_env.get("PATH", ""):
            phone_env["PATH"] = py_dir + os.pathsep + phone_env.get("PATH", "")
        print("[4/4] 运行 phone（约 45s）...", flush=True)
        t0 = time.time()
        with PHONE_OUT.open("w", encoding="utf-8") as fh:
            phone = subprocess.run([PY, str(ROOT / "scripts" / "sim" / "run-phone.py")],
                                   cwd=str(ROOT), env=phone_env,
                                   stdout=fh, stderr=subprocess.STDOUT,
                                   timeout=240)
        summary["steps"]["phone_rc"] = phone.returncode
        summary["steps"]["phone_s"] = round(time.time() - t0, 1)
        print(f"[4/4] phone 退出 rc={phone.returncode}", flush=True)

        # 桥侧账目（必须在 kill 之前读）：/metrics 给出 down_frames / queue_drops 等，
        # 用来与手机侧"到达帧数"逐级对账 —— 判定音频是丢在我们这层还是模型本来就短。
        # desktop 模式没有本地桥（bridge 不存在），整块跳过 → summary 无 bridge_metrics。
        if plan["read_bridge_metrics"]:
            try:
                import urllib.request
                # 探本机端口必须显式绕代理：裸 urlopen 会信任 HTTP_PROXY，把 127.0.0.1 也交给代理，
                # 于是"桥是活的"被读成读取失败 ⇒ bridge_metrics=None ⇒ 下游 `or {}` ⇒ 字段整体缺失。
                # 证据 [loopback-proxy/fresh-process-cells]
                #   outputs/2026-09-19-urlopen-proxy-fresh-process-cells.txt
                #   sha256 34f4aaa1e6cdfec1f7dd3e76e695a784f504d1020d2280c8ae2fc516100f95dd
                # 契约锁：backend/tests/contract/test_loopback_probe_proxy_contract.py
                _loopback_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with _loopback_opener.open("http://127.0.0.1:19093/metrics", timeout=5) as resp:
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
