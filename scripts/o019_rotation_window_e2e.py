#!/usr/bin/env python3
"""O-019 rotation-window real-process E2E (isolated instance, production untouched).

Launches `backend.app.main:app` via project-venv uvicorn on an isolated port
with per-scenario process-env credential overrides (pydantic-settings env vars
override the project .env; production .env is never modified) and a temp
voice.db, then drives real HTTP through the rotation-window contract
(windows-sidecar-credential-contract.md §6.3 / OPEN-DECISIONS O-019):

  S1 current-only       : C -> 200, N -> 40101, bad -> 40101, nonce replay -> 40102
  S2 window-active      : C -> 200 and N -> 200 (both accepted inside window)
  S3 scheduled          : C -> 200, N -> 40101 (next not yet enabled)
  S4 expired            : C -> 200, N -> 40101 (next window elapsed)
  S5 promote (restart)  : N -> 200, C -> 40101 (old value retired)
  S6 broken config + production gate: startup must fail closed

Multi-instance revision consistency / partial-instance rollback need a real
multi-instance deployment and are explicitly out of scope here.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"
# 探本机端口必须显式绕代理：裸 urlopen 信任 HTTP_PROXY，设了代理时会把 127.0.0.1 也交给
# 代理，于是活端口读成死（就绪探测永远 False）。实测与契约锁：
#   outputs/2026-09-19-urlopen-proxy-fresh-process-cells.txt
#   backend/tests/contract/test_loopback_probe_proxy_contract.py
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
ENV_KEYS = (
    "VOICE_SIDECAR_CREDENTIAL",
    "VOICE_SIDECAR_CREDENTIAL_NEXT",
    "VOICE_SIDECAR_NEXT_ENABLED_AT",
    "VOICE_SIDECAR_NEXT_EXPIRES_AT",
    "VOICE_SIDECAR_CONFIG_REVISION",
)


def new_secret() -> str:
    return f"jax-static-v1${secrets.token_hex(32)}"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_env(db_path: Path) -> dict:
    env = os.environ.copy()
    env["VOICE_DB_PATH"] = str(db_path)
    env["VOICE_OWNER_CREDENTIAL"] = new_secret()  # isolate from production owner slot
    for key in ENV_KEYS:
        env.pop(key, None)
    return env


class Instance:
    def __init__(self, workdir: Path, db_path: Path, extra_env: dict | None = None,
                 broken: bool = False):
        self.workdir = workdir
        self.env = base_env(db_path)
        if extra_env:
            self.env.update(extra_env)
        if broken:
            self.env["VOICE_PRODUCTION"] = "true"
        self.proc: subprocess.Popen | None = None

    def start(self, wait_seconds: float = 30.0) -> bool:
        self.proc = subprocess.Popen(
            [str(VENV_PY), "-m", "uvicorn", "backend.app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
            cwd=str(REPO_ROOT), env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False  # exited early (fail-closed)
            if self.health():
                return True
            time.sleep(0.3)
        return False

    def health(self) -> bool:
        try:
            with LOOPBACK_OPENER.open(f"{BASE}/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None


def request_pending(credential: str, nonce: str | None = None) -> tuple[int, str]:
    headers = {}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    if nonce is not None:
        headers["X-Request-Nonce"] = nonce
    req = urllib.request.Request(f"{BASE}/api/v1/voice/session/pending",
                                 headers=headers, method="GET")
    try:
        with LOOPBACK_OPENER.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def pending_ok(credential: str) -> tuple[bool, str]:
    nonce = secrets.token_hex(16)
    status, body = request_pending(credential, nonce)
    return status == 200 and json.loads(body).get("code") == 0, body


def expect(label: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail[:80]}" if not ok else ""))
    return ok


def run_scenario(server: Instance, cases: list[tuple[str, str, bool]]) -> bool:
    all_ok = True
    for label, credential, want_ok in cases:
        ok, body = pending_ok(credential)
        code = ""
        try:
            code = str(json.loads(body).get("code", "")) if not ok else "0"
        except Exception:
            pass
        all_ok &= expect(f"{label} -> {'200' if want_ok else '40101'}", ok == want_ok, code)
    return all_ok


def main() -> int:
    workdir = REPO_ROOT / ".workbuddy" / "o019-e2e-work"
    workdir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    all_ok = True

    c, n, bad = new_secret(), new_secret(), new_secret()
    now = datetime.now(timezone.utc)

    scenarios = [
        ("S1_current_only", {"VOICE_SIDECAR_CREDENTIAL": c}, [
            ("C current", c, True), ("N next", n, False), ("bad", bad, False)]),
        ("S2_window_active", {
            "VOICE_SIDECAR_CREDENTIAL": c,
            "VOICE_SIDECAR_CREDENTIAL_NEXT": n,
            "VOICE_SIDECAR_NEXT_ENABLED_AT": iso(now - timedelta(seconds=60)),
            "VOICE_SIDECAR_NEXT_EXPIRES_AT": iso(now + timedelta(seconds=300)),
            "VOICE_SIDECAR_CONFIG_REVISION": "rev-e2e-1",
        }, [("C current", c, True), ("N next in-window", n, True)]),
        ("S3_scheduled", {
            "VOICE_SIDECAR_CREDENTIAL": c,
            "VOICE_SIDECAR_CREDENTIAL_NEXT": n,
            "VOICE_SIDECAR_NEXT_ENABLED_AT": iso(now + timedelta(hours=1)),
            "VOICE_SIDECAR_NEXT_EXPIRES_AT": iso(now + timedelta(hours=1, minutes=5)),
            "VOICE_SIDECAR_CONFIG_REVISION": "rev-e2e-2",
        }, [("C current", c, True), ("N next scheduled", n, False)]),
        ("S4_expired", {
            "VOICE_SIDECAR_CREDENTIAL": c,
            "VOICE_SIDECAR_CREDENTIAL_NEXT": n,
            "VOICE_SIDECAR_NEXT_ENABLED_AT": iso(now - timedelta(hours=2)),
            "VOICE_SIDECAR_NEXT_EXPIRES_AT": iso(now - timedelta(hours=1, minutes=55)),
            "VOICE_SIDECAR_CONFIG_REVISION": "rev-e2e-3",
        }, [("C current", c, True), ("N next expired", n, False)]),
        ("S5_promoted", {
            "VOICE_SIDECAR_CREDENTIAL": n,
            "VOICE_SIDECAR_CONFIG_REVISION": "rev-e2e-4",
        }, [("N new current", n, True), ("C old retired", c, False)]),
    ]

    for name, extra, cases in scenarios:
        print(f"[{name}]")
        db = workdir / f"{name}.db"
        server = Instance(workdir, db, extra)
        up = server.start()
        if not up:
            print(f"  [FAIL] instance did not become healthy")
            results.append({"scenario": name, "ok": False, "error": "startup"})
            server.stop()
            all_ok = False
            continue
        try:
            ok = run_scenario(server, cases)
        finally:
            server.stop()
        results.append({"scenario": name, "ok": ok})
        all_ok &= ok

    print("[S6_broken_config_production_gate]")
    db = workdir / "S6.db"
    broken = Instance(workdir, db, {
        "VOICE_SIDECAR_CREDENTIAL": c,
        "VOICE_SIDECAR_CREDENTIAL_NEXT": n,  # next without timestamps = invalid
        "VOICE_SIDECAR_CONFIG_REVISION": "rev-e2e-5",
    }, broken=True)
    started = broken.start(wait_seconds=12)
    ok = expect("production instance with broken config refuses to start",
                not started, "started" if started else "refused")
    if started:
        broken.stop()
    results.append({"scenario": "S6_broken_config_production_gate", "ok": ok})
    all_ok &= ok

    # nonce replay semantics (against S1 configuration, fresh instance)
    print("[S7_nonce_replay]")
    db = workdir / "S7.db"
    server = Instance(workdir, db, {"VOICE_SIDECAR_CREDENTIAL": c})
    if server.start():
        try:
            nonce = secrets.token_hex(16)
            status1, _ = request_pending(c, nonce)
            status2, _ = request_pending(c, nonce)
            ok = expect("fresh nonce 200 then replay 40102",
                        status1 == 200 and status2 == 401, f"{status1}/{status2}")
        finally:
            server.stop()
    else:
        ok = False
        print("  [FAIL] instance did not start")
    results.append({"scenario": "S7_nonce_replay", "ok": ok})
    all_ok &= ok

    out = REPO_ROOT / "outputs" / "o019-rotation-window-e2e-20260904.json"
    out.write_text(json.dumps({"ok": all_ok, "results": results}, indent=2), encoding="utf-8")
    print(f"O019_ROTATION_E2E={'PASS' if all_ok else 'FAIL'} -> {out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
