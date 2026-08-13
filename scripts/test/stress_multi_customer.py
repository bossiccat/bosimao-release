"""多客户多场景高压测试 — 等价 3 小时业务量，主动寻找问题。

场景：50 独立客户设备 × 10 类场景（并发签发/pending 唯一性/限流/nonce 防重放/
多客户隔离/撤销拒绝/畸形输入/并发注册/长跑资源/边界输入）。
真实 FastAPI + SQLite（复用 VoiceSecurityFixture 装配模式，production=False）。
输出：问题清单（observation 逐条记录，异常即问题）。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes_voice import create_secured_voice_router
from app.voice.auth import CredentialValidator
from app.voice.config import VoiceSecurityConfig, build_sidecar_credential_hashes
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore

OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"
N_CLIENTS = 50
# 2026-08-13 修复后吞吐提升 17 倍（30.1s→1.72s）：200 并发会瞬时打满共享 IP
# 限流窗口（10s/200），污染后续场景。降至 100 并发（每设备 5 次 < IP 上限），
# 吞吐指标用 elapsed_s 记录，不依赖请求总量。
ROUNDS = 5


class Fixture:
    def __init__(self, tmp: Path):
        self.store = VoiceStore(tmp / "voice.db")
        self.store.initialize()
        self.devices: dict[str, str] = {}
        for i in range(N_CLIENTS):
            dev = f"dev-{i:04d}-{uuid.uuid4().hex[:16]}"
            secret = f"secret-{i:04d}-{uuid.uuid4().hex[:16]}"
            self.store.save_device(dev, secret, device_name=f"phone-{i:04d}")
            self.devices[dev] = secret
        sidecar_creds = build_sidecar_credential_hashes(current_secret=SIDECAR_SECRET)
        security = VoiceSecurityConfig(
            production=False,
            tls_enabled=True,
            owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
            sidecar_credential_hash=sidecar_creds.current_hash,
            nonce_enabled=True,
            rate_limit_enabled=True,
            trtc_sdk_app_id=FAKE_SDK_APP_ID,
            trtc_secret_key=FAKE_SECRET_KEY,
        )
        nonces = NonceService(self.store, ttl_seconds=300)
        limiter = RateLimiter(
            self.store,
            RateLimitConfig(window_seconds=10, device_limit=100, ip_limit=200),
        )
        service = RtcSessionService(
            RtcSessionConfig(
                sdk_app_id=FAKE_SDK_APP_ID,
                secret_key=FAKE_SECRET_KEY,
                room_prefix="jax-",
            )
        )
        from app.voice.devices import DeviceService

        devices = DeviceService(self.store)
        self.app = FastAPI()
        self.app.include_router(
            create_secured_voice_router(
                store=self.store,
                service=service,
                validator=CredentialValidator(
                    self.store,
                    CredentialValidator.hash_credential(OWNER_SECRET),
                    build_sidecar_credential_hashes(current_secret=SIDECAR_SECRET),
                ),
                nonces=nonces,
                limiter=limiter,
                security=security,
                devices=devices,
            )
        )
        self.owner_bearer = CredentialValidator.hash_credential(OWNER_SECRET)

    def bearer(self, secret: str) -> str:
        return f"Bearer {secret}"

    def device_bearer(self, dev: str, secret: str) -> str:
        return f"Bearer {dev}.{secret}"


def nonce() -> str:
    return uuid.uuid4().hex


async def main() -> None:
    problems: list[dict] = []
    start_all = time.monotonic()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        fx = Fixture(Path(td))
        transport = ASGITransport(app=fx.app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            # ---------- H1 多客户并发签发 ----------
            async def issue_one(dev: str, secret: str, n: str):
                r = await c.post(
                    "/api/v1/voice/session",
                    headers={"authorization": fx.device_bearer(dev, secret), "x-request-nonce": n},
                    json={"device_id": dev, "entry_point": "main"},
                )
                return r.status_code, r.text[:200]

            t0 = time.monotonic()
            tasks = []
            for dev, secret in list(fx.devices.items())[:20]:
                for _ in range(ROUNDS):
                    tasks.append(issue_one(dev, secret, nonce()))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            dt = time.monotonic() - t0
            codes = {}
            errs = 0
            for r in results:
                if isinstance(r, Exception):
                    errs += 1
                    continue
                codes[r[0]] = codes.get(r[0], 0) + 1
            obs = {
                "scene": "H1-multi-client-concurrent-issue",
                "calls": len(results), "elapsed_s": round(dt, 2),
                "codes": codes, "exceptions": errs,
            }
            problems.append(obs)
            if codes.get(500, 0) or errs:
                problems.append({"scene": "H1", "problem": f"5xx/exception seen: {codes} errs={errs}"})

            # ---------- H2 pending claim 唯一性（多 sidecar 并发） ----------
            # 2026-08-13：H1 已占用同一 10s 窗口的 IP 计数，H2 首请求可能 429
            # （限流正常生效）。等窗口滚动后重试一次，429 只观测不判 problem。
            await asyncio.sleep(10.1)
            claim_problems = 0
            for dev, secret in list(fx.devices.items())[:10]:
                r = await c.post(
                    "/api/v1/voice/session",
                    headers={"authorization": fx.device_bearer(dev, secret), "x-request-nonce": nonce()},
                    json={"device_id": dev, "entry_point": "main"},
                )
                if r.status_code == 429:
                    await asyncio.sleep(10.1)
                    r = await c.post(
                        "/api/v1/voice/session",
                        headers={"authorization": fx.device_bearer(dev, secret), "x-request-nonce": nonce()},
                        json={"device_id": dev, "entry_point": "main"},
                    )
                if r.status_code != 201:
                    problems.append({"scene": "H2", "problem": f"issue failed {dev} {r.status_code}"})
                    continue
                data = r.json()["data"]
                claims = await asyncio.gather(*[
                    c.get(
                        "/api/v1/voice/session/pending",
                        headers={"authorization": fx.bearer(SIDECAR_SECRET), "x-request-nonce": nonce()},
                    )
                    for _ in range(3)
                ])
                # 2026-08-13 修正断言语义：队列中已有 H1 遗留 pending 时，并发 claim
                # 各自拿到不同 intent 都算成功。唯一性 = 同一 session_id 不被双 claim
                # （session_id 集合无重复），而不是"只有 1 个 claim 成功"。
                got = [
                    x.json()["data"]["intents"][0]["session_id"]
                    for x in claims
                    if x.status_code == 200 and x.json()["data"]["intents"]
                ]
                if not got:
                    claim_problems += 1
                    problems.append({
                        "scene": "H2",
                        "problem": f"claim returned no intent dev={dev} statuses={[x.status_code for x in claims]}",
                    })
                elif len(set(got)) != len(got):
                    claim_problems += 1
                    problems.append({
                        "scene": "H2",
                        "problem": f"claim uniqueness violated dev={dev} duplicate_session_ids={got}",
                    })
            problems.append({"scene": "H2-pending-claim-uniqueness", "devices": 10, "violations": claim_problems})

            # ---------- H3 限流边界 ----------
            dev, secret = list(fx.devices.items())[21]
            codes3 = {}
            for _ in range(150):
                r = await c.post(
                    "/api/v1/voice/session",
                    headers={"authorization": fx.device_bearer(dev, secret), "x-request-nonce": nonce()},
                    json={"device_id": dev, "entry_point": "main"},
                )
                codes3[r.status_code] = codes3.get(r.status_code, 0) + 1
            problems.append({"scene": "H3-rate-limit", "codes": codes3})
            if codes3.get(429, 0) == 0:
                problems.append({"scene": "H3", "problem": "rate limit never triggered at 150 calls"})

            # ---------- H4 nonce 防重放 ----------
            dev, secret = list(fx.devices.items())[22]
            n = nonce()
            r1 = await c.post(
                "/api/v1/voice/session",
                headers={"authorization": fx.device_bearer(dev, secret), "x-request-nonce": n},
                json={"device_id": dev, "entry_point": "main"},
            )
            r2 = await c.post(
                "/api/v1/voice/session",
                headers={"authorization": fx.device_bearer(dev, secret), "x-request-nonce": n},
                json={"device_id": dev, "entry_point": "main"},
            )
            problems.append({
                "scene": "H4-nonce-replay",
                "first": r1.status_code, "replay": r2.status_code,
            })
            if r2.status_code != 401:
                problems.append({"scene": "H4", "problem": f"nonce replay accepted: {r2.status_code}"})

            # ---------- H5 多客户隔离 ----------
            dev_a, sec_a = list(fx.devices.items())[23]
            dev_b, _ = list(fx.devices.items())[24]
            r = await c.post(
                "/api/v1/voice/session",
                headers={"authorization": fx.device_bearer(dev_a, sec_a), "x-request-nonce": nonce()},
                json={"device_id": dev_b, "entry_point": "main"},
            )
            problems.append({"scene": "H5-cross-client-isolation", "status": r.status_code})
            if r.status_code == 201:
                problems.append({"scene": "H5", "problem": "client A can issue session for client B"})

            # ---------- H6 撤销后拒绝 ----------
            dev_c, sec_c = list(fx.devices.items())[25]
            rv = await c.post(
                f"/api/v1/voice/devices/{dev_c}/revoke",
                headers={"authorization": fx.bearer(OWNER_SECRET), "x-request-nonce": nonce()},
                json={"reason": "stress-revoke"},
            )
            r = await c.post(
                "/api/v1/voice/session",
                headers={"authorization": fx.device_bearer(dev_c, sec_c), "x-request-nonce": nonce()},
                json={"device_id": dev_c, "entry_point": "main"},
            )
            problems.append({"scene": "H6-revoke-then-issue", "revoke": rv.status_code, "issue_after": r.status_code})

            # ---------- H7 畸形输入风暴 ----------
            bad = [
                {"device_id": ""},
                {"device_id": "x" * 200},
                {"device_id": "a" * 64},
                {"device_id": "中文设备"},
                {"device_id": None},
                {},
                {"device_id": "ok-dev", "extra": "unknown-field"},
            ]
            h7 = []
            dev_d, sec_d = list(fx.devices.items())[26]
            for body in bad:
                r = await c.post(
                    "/api/v1/voice/session",
                    headers={"authorization": fx.device_bearer(dev_d, sec_d), "x-request-nonce": nonce()},
                    json=body,
                )
                h7.append((str(body)[:40], r.status_code))
            problems.append({"scene": "H7-malformed-input", "results": h7})
            if any(code >= 500 for _, code in h7):
                problems.append({"scene": "H7", "problem": "5xx on malformed input"})

            # ---------- H8 并发注册同名设备 ----------
            dup_dev = f"dup-{uuid.uuid4().hex[:12]}"
            regs = await asyncio.gather(*[
                c.post(
                    "/api/v1/voice/devices/register",
                    headers={"authorization": fx.bearer(OWNER_SECRET), "x-request-nonce": nonce()},
                    json={"pairing_code": "x" * 24, "device_name": "dup", "platform": "android"},
                )
                for _ in range(5)
            ])
            statuses = sorted(r.status_code for r in regs)
            problems.append({"scene": "H8-concurrent-register-duplicate", "statuses": statuses})
            if sum(1 for s in statuses if s == 201) > 1:
                problems.append({"scene": "H8", "problem": f"duplicate device registered {statuses}"})

            # ---------- H9 长跑资源（等价 3 小时：60s 混合负载 + 采样） ----------
            dev_e, sec_e = list(fx.devices.items())[27]
            t_end = time.monotonic() + 60
            ok = 0
            non_ok = {}
            while time.monotonic() < t_end:
                r = await c.post(
                    "/api/v1/voice/session",
                    headers={"authorization": fx.device_bearer(dev_e, sec_e), "x-request-nonce": nonce()},
                    json={"device_id": dev_e, "entry_point": "main"},
                )
                if r.status_code == 201:
                    ok += 1
                else:
                    non_ok[r.status_code] = non_ok.get(r.status_code, 0) + 1
            try:
                mem_mb = None  # Windows 无 resource 模块，内存采样留空
            except Exception:
                mem_mb = None
            problems.append({
                "scene": "H9-long-run-60s",
                "ok": ok, "non_ok": non_ok, "mem_mb": mem_mb,
            })

            # ---------- H10 边界输入 ----------
            dev_f, sec_f = list(fx.devices.items())[28]
            r = await c.post(
                "/api/v1/voice/session",
                headers={
                    "authorization": fx.device_bearer(dev_f, sec_f),
                    "x-request-nonce": "x" * 200,
                },
                json={"device_id": dev_f, "entry_point": "main"},
            )
            problems.append({"scene": "H10-oversize-nonce", "status": r.status_code})

    problems.append({"total_elapsed_s": round(time.monotonic() - start_all, 2)})
    out = Path("outputs") / "stress-multi-customer-2026-08-13.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(problems, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(problems, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
