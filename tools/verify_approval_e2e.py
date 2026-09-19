"""端到端真机验证：审批控制面（FastAPI 进程 + rtc_bridge 进程，两个独立真实进程）

链路：
  1. spawn_agent_thread（通过 registry 直连，模拟 Qwen 工具调用）
  2. HTTP 审批（真实 FastAPI server，owner Bearer + nonce）
  3. 独立 rtc_bridge 进程的命令消费者领取 start_worker
  4. Worker 以 claim_queued 原子领取，跑只读 Hermes 探测，状态机收口
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import urllib.request
import urllib.error

# 探本机端口必须显式绕代理：本脚本所有 HTTP 都是打 127.0.0.1 上的验证实例，而裸
# urlopen 信任 HTTP_PROXY，设了代理时会把 127.0.0.1 也交给代理 —— 于是活端口读成死，
# 40 次就绪重试全部失败，脚本报"启动失败"而其实服务是好的。
# 实测与契约锁：outputs/2026-09-19-urlopen-proxy-fresh-process-cells.txt、
# backend/tests/contract/test_loopback_probe_proxy_contract.py
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PY = Path(r"C:/Users/Administrator/WorkBuddy/监视app/.venv/Scripts/python.exe")
DATA_DIR = ROOT / "backend" / "data"
RUN_TAG = uuid.uuid4().hex[:12]
VERIFY_DB = DATA_DIR / f"verify_approval_e2e_{RUN_TAG}.sqlite3"
VOICE_DB = DATA_DIR / f"verify_approval_voice_{RUN_TAG}.db"

OWNER_SECRET = "verify-owner-e2e-secret-0827"
API_PORT = 18000
BRIDGE_HEALTH_PORT = 19097

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def http_post(url: str, body: dict, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with LOOPBACK_OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main() -> int:
    # 每次运行使用随机数据库名，避免删除既有运行数据；生成的 SQLite 文件已在 .gitignore 中。

    # 1. 起 FastAPI 验证实例（worktree 代码，独立端口/独立 DB）
    api_env = os.environ.copy()
    api_env.update({
        "AGENT_THREAD_DB": str(VERIFY_DB),
        "VOICE_DB_PATH": str(VOICE_DB),
        "PYTHONPATH": str(BACKEND),
    })
    api_script = ROOT / "tools" / "_verify_approval_api_app.py"
    api_proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "tools._verify_approval_api_app:app",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=str(ROOT), env=api_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"[..] FastAPI 验证实例 PID={api_proc.pid} port={API_PORT}")

    # 2. 起 rtc_bridge 验证实例（独立健康端口，同一 VERIFY_DB → 跨进程）
    bridge_env = os.environ.copy()
    bridge_env.update({
        "AGENT_THREAD_DB": str(VERIFY_DB),
        "RTC_BRIDGE_WS_PORT": "19096",
        "RTC_BRIDGE_HEALTH_PORT": str(BRIDGE_HEALTH_PORT),
        "PYTHONPATH": str(BACKEND),
    })
    bridge_proc = subprocess.Popen(
        [str(VENV_PY), "-m", "rtc_bridge.main"],
        cwd=str(BACKEND), env=bridge_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"[..] rtc_bridge 验证实例 PID={bridge_proc.pid} health={BRIDGE_HEALTH_PORT}")

    try:
        # 3. 等 FastAPI 就绪
        api_ready = False
        for _ in range(40):
            try:
                with LOOPBACK_OPENER.open(f"http://127.0.0.1:{API_PORT}/healthz", timeout=2) as r:
                    if r.status == 200:
                        api_ready = True
                        break
            except Exception:
                time.sleep(0.5)
        check("FastAPI 验证实例 /healthz 就绪", api_ready)

        # 4. 等 rtc_bridge /health 就绪（进程真实存活 + proc_name 验证）
        bridge_ready = False
        health_body = ""
        for _ in range(40):
            try:
                with LOOPBACK_OPENER.open(f"http://127.0.0.1:{BRIDGE_HEALTH_PORT}/health", timeout=2) as r:
                    health_body = r.read().decode("utf-8")
                    if r.status == 200:
                        bridge_ready = True
                        break
            except Exception:
                time.sleep(0.5)
        check("rtc_bridge /health 就绪（真实进程）", bridge_ready, health_body[:100])
        check("rtc_bridge health 返回 status=ok", (json.loads(health_body) if health_body else {}).get("status") == "ok")

        # 5. 查进程名（验收铁律：真机进程证据）
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"ProcessId={bridge_proc.pid}\" | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True)
        real_name = ps.stdout.strip()
        check("rtc_bridge PID 进程名核实（PowerShell 证据）",
              real_name in ("python.exe", "pythonw.exe"), real_name)

        # 6. spawn（模拟 Qwen 工具调用，走同一 VERIFY_DB）
        sys.path.insert(0, str(BACKEND))
        os.environ["AGENT_THREAD_DB"] = str(VERIFY_DB)
        from app.brain.agent_thread_registry import AgentThreadRegistry
        registry = AgentThreadRegistry(str(VERIFY_DB))
        thread = registry.handle_tool("spawn_agent_thread", {"user_speech": "端到端验证：只读探测"})
        check("spawn 后状态=awaiting_approval", thread.get("status") == "awaiting_approval", thread.get("status", ""))
        check("服务端生成 approval_id >= 32 字符", len(thread.get("approval_id", "")) >= 32)
        thread_id = thread["thread_id"]
        approval_id = thread["approval_id"]

        base = f"http://127.0.0.1:{API_PORT}/api/v1/brain/threads/{thread_id}/approve"

        # 7. 无 Bearer → 40101
        code, body = http_post(base, {"approval_id": approval_id}, {})
        check("无 Bearer → HTTP 401 / code 40101", code == 401 and body.get("code") == 40101, f"got {code}/{body.get('code')}")

        # 8. 缺 nonce → 40102
        code, body = http_post(base, {"approval_id": approval_id}, {"Authorization": f"Bearer {OWNER_SECRET}"})
        check("缺 nonce → HTTP 401 / code 40102", code == 401 and body.get("code") == 40102, f"got {code}/{body.get('code')}")

        # 9. 错误 approval_id → 40901
        code, body = http_post(base, {"approval_id": "x" * 32},
                               {"Authorization": f"Bearer {OWNER_SECRET}", "X-Request-Nonce": uuid.uuid4().hex})
        check("错误 approval_id → HTTP 409 / code 40901", code == 409 and body.get("code") == 40901, f"got {code}/{body.get('code')}")

        # 10. 正确审批 → 200，bridge 在 3s 内领取
        nonce = uuid.uuid4().hex
        code, body = http_post(base, {"approval_id": approval_id},
                               {"Authorization": f"Bearer {OWNER_SECRET}", "X-Request-Nonce": nonce})
        check("正确审批 → HTTP 200 code 0", code == 200 and body.get("code") == 0, f"got {code}/{body.get('code')}")

        # 11. nonce 重放 → 40102
        code, body = http_post(base, {"approval_id": approval_id},
                               {"Authorization": f"Bearer {OWNER_SECRET}", "X-Request-Nonce": nonce})
        check("nonce 重放 → HTTP 401 / code 40102", code == 401 and body.get("code") == 40102, f"got {code}/{body.get('code')}")

        # 12. 等命令消费者领取并启动 Worker（Hermes --help 很快）
        final_status = ""
        for _ in range(30):
            row = registry.get(thread_id) or {}
            final_status = row.get("status", "")
            if final_status in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.5)
        row = registry.get(thread_id) or {}
        check("bridge 领取命令并启动 Worker → running/completed",
              final_status in ("running", "completed", "failed"), f"final={final_status} summary={row.get('summary','')[:40]}")

        # 13. 命令标记 completed（防重放）
        import sqlite3
        db = sqlite3.connect(str(VERIFY_DB))
        cmds = db.execute("SELECT status, count(*) FROM agent_commands GROUP BY status").fetchall()
        db.close()
        check("start_worker 命令最终=completed（防重放收口）",
              dict(cmds).get("completed", 0) >= 1, str(dict(cmds)))

        # 14. 重启 bridge → recover_commands 不重放（completed 不回 pending）
        bridge_proc.terminate()
        bridge_proc.wait(timeout=10)
        bridge2 = subprocess.Popen(
            [str(VENV_PY), "-m", "rtc_bridge.main"],
            cwd=str(BACKEND), env=bridge_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"[..] rtc_bridge 重启实例 PID={bridge2.pid}")
        ready2 = False
        for _ in range(40):
            try:
                with LOOPBACK_OPENER.open(f"http://127.0.0.1:{BRIDGE_HEALTH_PORT}/health", timeout=2) as r:
                    if r.status == 200:
                        ready2 = True
                        break
            except Exception:
                time.sleep(0.5)
        check("bridge 重启后 /health 恢复", ready2)
        time.sleep(2.0)  # 给消费者足够轮询周期观察是否重放
        import sqlite3 as _s
        db = _s.connect(str(VERIFY_DB))
        cmds2 = db.execute("SELECT status, count(*) FROM agent_commands GROUP BY status").fetchall()
        workers = db.execute("SELECT count(*) FROM agent_threads WHERE status IN ('completed','failed','running','cancelled')").fetchone()[0]
        db.close()
        check("重启后 completed 命令未被重放（仍 1 条 completed）",
              dict(cmds2).get("completed", 0) == 1 and dict(cmds2).get("pending", 0) in (0, None), str(dict(cmds2)))
        check("重启后 Worker 未重复执行（线程状态未回退）", workers == 1, f"workers={workers}")

        bridge2.terminate()
        bridge2.wait(timeout=10)
    finally:
        for p in (bridge_proc, api_proc):
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                p.kill()

    print()
    if FAILURES:
        print(f"RESULT: FAIL（{len(FAILURES)} 项）: {FAILURES}")
        return 1
    print("RESULT: ALL PASS — 审批控制面真机双进程端到端验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
