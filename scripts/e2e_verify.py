"""e2e 端到端验收（每版本跑）：
V1: 后端健康 → 状态接口 → 授权状态接口 → 配置热重载生效 → 触发测试提醒(WS alert 事件) → 推送可达性
用法: python scripts/e2e_verify.py
说明: 无 webhook(.env 未配置 WECOM_WEBHOOK_URL/NTFY_TOPIC) 时推送项 SKIP 并标注。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000/ws/pet"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETECTION_YAML = PROJECT_ROOT / "config" / "detection.yaml"

CHECKS: list[tuple[str, bool, str]] = []
SKIPPED = 0


def check(name: str, ok: bool, detail: str = "", skip: bool = False) -> None:
    global SKIPPED
    if skip:
        SKIPPED += 1
        print(f"  [SKIP] {name} — {detail}")
        return
    CHECKS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def _read_env(key: str) -> str:
    """读取 .env 中的环境变量（e2e 不依赖 shell env）"""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(key, "")


def check_health(client: httpx.Client) -> bool:
    try:
        h = client.get(f"{BASE}/health")
        ok = h.status_code == 200
        check("后端健康", ok, h.text[:60])
        return ok
    except httpx.HTTPError as e:
        check("后端健康", False, f"无法连接: {e}")
        print("请先启动后端: cd backend && .venv/Scripts/python -m uvicorn app.main:app --port 8000")
        return False


def check_status(client: httpx.Client) -> None:
    try:
        s = client.get(f"{BASE}/api/v1/status")
        data = s.json()
        ok = s.status_code == 200 and "sessions" in data
        has_config = "config" in data and "detection" in data.get("config", {})
        check(
            "状态接口",
            ok and has_config,
            f"{len(data.get('sessions', []))} 会话, config={has_config}",
        )
    except Exception as e:  # noqa: BLE001
        check("状态接口", False, str(e))


def check_capture_status(client: httpx.Client) -> None:
    try:
        cs = client.get(f"{BASE}/api/v1/capture/status")
        csd = cs.json()
        sessions = csd.get("sessions", [])
        ok = cs.status_code == 200 and "sessions" in csd
        modes = [s.get("mode") for s in sessions]
        auths = [s.get("auth_status") for s in sessions]
        check(
            "授权状态接口",
            ok,
            f"{len(sessions)} 会话 modes={modes} auth={auths}",
        )
    except Exception as e:  # noqa: BLE001
        check("授权状态接口", False, str(e))


def check_config_reload(client: httpx.Client) -> None:
    """改 detection.yaml 阈值 → POST /config/reload → 状态接口返回新值 → 还原"""
    try:
        orig = DETECTION_YAML.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        check("配置改值生效", False, f"读取 detection.yaml 失败: {e}")
        return
    m = re.search(r"stuck_frame_threshold:\s*(\d+)", orig)
    old = int(m.group(1)) if m else 3
    new_threshold = old + 1
    try:
        patched = re.sub(
            r"(stuck_frame_threshold:\s*)\d+",
            rf"\g<1>{new_threshold}",
            orig,
            count=1,
        )
        DETECTION_YAML.write_text(patched, encoding="utf-8")
        r = client.post(f"{BASE}/api/v1/config/reload")
        reload_ok = r.status_code == 200 and r.json().get("ok") is True
        s = client.get(f"{BASE}/api/v1/status")
        got = s.json().get("config", {}).get("detection", {}).get("stuck_frame_threshold")
        check(
            "配置改值生效",
            reload_ok and got == new_threshold,
            f"改阈值 {old}→{new_threshold}，状态接口返回={got}",
        )
    except Exception as e:  # noqa: BLE001
        check("配置改值生效", False, str(e))
    finally:
        try:
            DETECTION_YAML.write_text(orig, encoding="utf-8")
            client.post(f"{BASE}/api/v1/config/reload")
        except Exception:  # noqa: BLE001
            pass


async def _ws_alert_check(client: httpx.Client) -> None:
    """WS 订阅 + 触发测试提醒 → 断言收到 alert 事件"""
    try:
        async with websockets.connect(WS_URL) as ws:
            r = client.post(
                f"{BASE}/api/v1/control",
                json={"action": "trigger_alert_test", "target": "codex"},
            )
            accepted = r.status_code in (200, 202)
            got_alert = False
            alert_data = None
            deadline = time.time() + 8.0
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "event" and msg.get("event") == "alert":
                    got_alert = True
                    alert_data = msg.get("data")
                    break
            check(
                "触发提醒→alert 事件",
                accepted and got_alert,
                f"accepted={accepted} alert={got_alert} level={alert_data and alert_data.get('level')}",
            )
    except Exception as e:  # noqa: BLE001
        check("触发提醒→alert 事件", False, str(e))


def check_push(client: httpx.Client) -> None:
    has_wecom = bool(_read_env("WECOM_WEBHOOK_URL"))
    has_ntfy = bool(_read_env("NTFY_TOPIC"))
    if not has_wecom and not has_ntfy:
        check("推送可达性", False, "无 webhook（.env 未配置），跳过", skip=True)
        return
    try:
        p = client.post(f"{BASE}/api/v1/control/test-push")
        pd = p.json()
        check(
            "推送可达性",
            pd.get("ok", False),
            f"provider={pd.get('provider')} err={pd.get('error')}",
        )
    except Exception as e:  # noqa: BLE001
        check("推送可达性", False, str(e))


def main() -> int:
    print("=== 贾克斯模式 e2e 验收 ===")
    with httpx.Client(timeout=15.0) as c:
        if not check_health(c):
            return 1
        check_status(c)
        check_capture_status(c)
        check_config_reload(c)
        asyncio.run(_ws_alert_check(c))
        check_push(c)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    print(f"\n=== 结果: {passed}/{len(CHECKS)} 通过, {failed} 失败, {SKIPPED} 跳过 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
