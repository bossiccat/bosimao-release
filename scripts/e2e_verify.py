"""e2e 端到端验收（每版本跑）：
V1: 后端健康 → 状态接口 → 控制指令(测试提醒) → 推送可达性
用法: python scripts/e2e_verify.py
"""
from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8000"
CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=== 贾克斯模式 e2e 验收 ===")
    with httpx.Client(timeout=10.0) as c:
        try:
            h = c.get(f"{BASE}/health")
            check("后端健康", h.status_code == 200, h.text[:60])
        except httpx.HTTPError as e:
            check("后端健康", False, f"无法连接: {e}")
            print("请先启动后端: python -m uvicorn app.main:app --port 8000")
            return 1

        try:
            s = c.get(f"{BASE}/api/v1/status")
            data = s.json()
            check("状态接口", s.status_code == 200 and "sessions" in data,
                  f"{len(data.get('sessions', []))} 个会话")
        except Exception as e:  # noqa: BLE001
            check("状态接口", False, str(e))

        try:
            p = c.post(f"{BASE}/api/v1/control/test-push")
            pd = p.json()
            check("推送可达性", pd.get("ok", False),
                  f"provider={pd.get('provider')} err={pd.get('error')}")
        except Exception as e:  # noqa: BLE001
            check("推送可达性", False, str(e))

        try:
            r = c.post(f"{BASE}/api/v1/control", json={"action": "trigger_alert_test"})
            check("触发测试提醒", r.status_code == 202 or r.status_code == 200, r.text[:80])
        except Exception as e:  # noqa: BLE001
            check("触发测试提醒", False, str(e))

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n=== 结果: {passed}/{len(CHECKS)} 通过 ===")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
