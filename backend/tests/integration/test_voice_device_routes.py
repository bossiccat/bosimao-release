"""设备生命周期与撤销集成验收测试（计划 Task 4 / QA spec 5.4）

真实 FastAPI secured router + DeviceService + 临时 SQLite；禁止 mock-only。
覆盖：owner pairing-code、TTL<=300、只存 code_hash、并发注册恰好一个成功、
过期/已消费拒绝、Secret 只返回一次、列表不含 Secret、撤销幂等、撤销后 credential
立即拒绝、活动 session 终止、userSig 指纹入撤销表、其他设备不受影响、撤销失败明确可重试。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import create_secured_voice_router
from app.voice.auth import CredentialValidator
from app.voice.config import VoiceSecurityConfig
from app.voice.devices import DeviceService
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore
from app.voice.usersig import parse_user_sig

OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"


def _scan_for(store: VoiceStore, secrets: list[str]) -> list[str]:
    raw = Path(store.db_path).read_bytes()
    return [s for s in secrets if s.encode("latin1", errors="ignore") in raw]


class _ConfirmingTerminator:
    def terminate_and_wait(self, device_id: str, session_ids: list[str]) -> list[str]:
        return list(session_ids)


class _Fixture:
    def __init__(self, tmp_path: Path, device_limit: int = 1000, ip_limit: int = 1000):
        self.store = VoiceStore(tmp_path / "voice.db")
        self.store.initialize()
        security = VoiceSecurityConfig(
            production=False,
            tls_enabled=True,
            owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
            sidecar_credential_hash=CredentialValidator.hash_credential(SIDECAR_SECRET),
            nonce_enabled=True,
            rate_limit_enabled=True,
            trtc_sdk_app_id=FAKE_SDK_APP_ID,
            trtc_secret_key=FAKE_SECRET_KEY,
        )
        self.security = security
        self.validator = CredentialValidator(
            self.store, security.owner_credential_hash, security.sidecar_credential_hash
        )
        self.nonces = NonceService(self.store, ttl_seconds=300)
        self.limiter = RateLimiter(
            self.store,
            RateLimitConfig(window_seconds=60, device_limit=device_limit, ip_limit=ip_limit),
        )
        self.service = RtcSessionService(
            RtcSessionConfig(sdk_app_id=FAKE_SDK_APP_ID, secret_key=FAKE_SECRET_KEY, room_prefix="jax-")
        )
        self.devices = DeviceService(self.store, terminator=_ConfirmingTerminator())
        self.app = FastAPI()
        self.app.include_router(
            create_secured_voice_router(
                store=self.store,
                service=self.service,
                validator=self.validator,
                nonces=self.nonces,
                limiter=self.limiter,
                security=security,
                devices=self.devices,
            )
        )
        self.client = TestClient(self.app)

    def owner_headers(self, nonce: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {OWNER_SECRET}"}
        if nonce is not None:
            headers["X-Request-Nonce"] = nonce
        return headers

    def device_headers(self, device_id: str, secret: str, nonce: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {device_id}.{secret}"}
        if nonce is not None:
            headers["X-Request-Nonce"] = nonce
        return headers


def _nonce() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def fx(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


def _create_pairing(fx: _Fixture) -> tuple[str, dict]:
    resp = fx.client.post(
        "/api/v1/voice/devices/pairing-code",
        json={"platform": "android", "device_name_hint": "phone-a"},
        headers=fx.owner_headers(_nonce()),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["pairing_code"], resp.json()["data"]


def _register(fx: _Fixture, code: str, device_name: str = "phone-a") -> dict:
    resp = fx.client.post(
        "/api/v1/voice/devices/register",
        json={"pairing_code": code, "device_name": device_name, "platform": "android"},
        headers={"X-Request-Nonce": _nonce()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# ---------- 1. pairing-code：owner 生成、TTL<=300、只存哈希 ----------

def test_pairing_code_requires_owner_bearer(fx: _Fixture) -> None:
    resp = fx.client.post(
        "/api/v1/voice/devices/pairing-code", json={"platform": "android"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_owner_pairing_code_ttl_bounded_and_hash_only(fx: _Fixture) -> None:
    code, meta = _create_pairing(fx)
    assert len(code) >= 20
    assert meta["max_uses"] == 1
    assert 1 <= meta["ttl_seconds"] <= 300
    assert meta["expires_at"] - time.time() <= 300
    assert _scan_for(fx.store, [code]) == []
    with fx.store.connect() as conn:
        row = conn.execute(
            "SELECT code_hash FROM pairing_codes WHERE created_by_owner_id='owner'"
        ).fetchone()
    assert row is not None
    assert row[0] == hashlib.sha256(code.encode()).hexdigest()


# ---------- 2. register：bootstrap 主体、Secret 一次性 ----------

def test_register_requires_nonce(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    resp = fx.client.post(
        "/api/v1/voice/devices/register",
        json={"pairing_code": code, "device_name": "phone-a", "platform": "android"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40102


def test_register_success_secret_returned_once_and_usable(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    data = _register(fx, code)
    assert data["device_id"]
    assert data["credential_id"]
    assert len(data["credential_secret"]) >= 32
    assert data["expires_at"] > time.time()
    # Secret 只展示一次：列表不含、DB 字节不含、服务端 API 不返回明文
    assert _scan_for(fx.store, [data["credential_secret"]]) == []
    # 新 credential 可直接签发会话（端到端）
    session = fx.client.post(
        "/api/v1/voice/session",
        json={"device_id": data["device_id"], "entry_point": "main"},
        headers=fx.device_headers(data["device_id"], data["credential_secret"], _nonce()),
    )
    assert session.status_code == 201
    assert session.json()["data"]["scene"] == "trtc_full_duplex"


def test_register_consumed_pairing_rejected_and_no_second_device(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    first = _register(fx, code, device_name="phone-a")
    second = fx.client.post(
        "/api/v1/voice/devices/register",
        json={"pairing_code": code, "device_name": "phone-b", "platform": "android"},
        headers={"X-Request-Nonce": _nonce()},
    )
    assert second.status_code == 409
    assert second.json()["code"] == 40901
    with fx.store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]
        consumed = conn.execute(
            "SELECT consumed_device_id FROM pairing_codes WHERE consumed_at IS NOT NULL"
        ).fetchone()
    assert count == 1
    assert consumed[0] == first["device_id"]


def test_register_expired_pairing_rejected(fx: _Fixture) -> None:
    code, meta = fx.devices.create_pairing_code("owner", "android", ttl_seconds=1,
                                                now=time.time() - 120)
    assert meta["ttl_seconds"] == 1
    resp = fx.client.post(
        "/api/v1/voice/devices/register",
        json={"pairing_code": code, "device_name": "phone-a", "platform": "android"},
        headers={"X-Request-Nonce": _nonce()},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40901
    with fx.store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]
    assert count == 0


def test_register_concurrent_exactly_one_succeeds(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def attempt(index: int) -> None:
        barrier.wait()
        resp = fx.client.post(
            "/api/v1/voice/devices/register",
            json={"pairing_code": code, "device_name": f"phone-{index}", "platform": "android"},
            headers={"X-Request-Nonce": _nonce()},
        )
        with lock:
            results.append((resp.status_code, resp.json().get("code", -1)))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count((201, 0)) == 1
    assert all(code == 40901 for status, code in results if status != 201)
    with fx.store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]
    assert count == 1


# ---------- 3. list：owner 专属、不含 Secret ----------

def test_list_devices_requires_owner_and_has_no_secret(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    _register(fx, code)
    # device credential 不能看列表（owner 隔离）
    dev = fx.client.get("/api/v1/voice/devices", headers=fx.device_headers("x", "y"))
    assert dev.status_code == 401
    assert dev.json()["code"] == 40101
    # owner 可看，但无 Secret/hash
    resp = fx.client.get("/api/v1/voice/devices", headers=fx.owner_headers())
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] >= 1
    item = body["items"][0]
    assert "credential_secret" not in item
    assert "credential_hash" not in item
    assert "secret" not in json.dumps(body)


# ---------- 4. revoke：强一致撤销 ----------

def test_revoke_requires_owner_and_nonce(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    data = _register(fx, code)
    resp = fx.client.post(
        f"/api/v1/voice/devices/{data['device_id']}/revoke",
        json={"reason": "lost"},
        headers=fx.owner_headers(),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40102
    resp2 = fx.client.post(
        f"/api/v1/voice/devices/{data['device_id']}/revoke",
        json={"reason": "lost"},
    )
    assert resp2.status_code == 401
    assert resp2.json()["code"] == 40101


def test_revoke_terminates_session_and_records_user_sig_fingerprint(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    reg = _register(fx, code)
    # 设备签发活动 session，拿到 userSig
    session = fx.client.post(
        "/api/v1/voice/session",
        json={"device_id": reg["device_id"], "entry_point": "main"},
        headers=fx.device_headers(reg["device_id"], reg["credential_secret"], _nonce()),
    )
    assert session.status_code == 201
    session_data = session.json()["data"]
    user_sig = session_data["user_sig"]
    fingerprint = hashlib.sha256(user_sig.encode()).hexdigest()

    revoke = fx.client.post(
        f"/api/v1/voice/devices/{reg['device_id']}/revoke",
        json={"reason": "lost phone"},
        headers=fx.owner_headers(_nonce()),
    )
    assert revoke.status_code == 200
    rv = revoke.json()["data"]
    assert rv["status"] == "revoked"
    assert rv["revoked_at"]
    assert session_data["session_id"] in rv["terminated_session_ids"]

    # userSig 指纹进入撤销表，reason 记录
    with fx.store.connect() as conn:
        row = conn.execute(
            "SELECT session_id, user_sig_fingerprint, reason, device_id"
            " FROM revoked_sessions WHERE device_id=?",
            (reg["device_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == session_data["session_id"]
    assert row[1] == fingerprint
    assert row[2] == "lost phone"
    # 撤销后 credential 立即拒绝（40103）
    after = fx.client.post(
        "/api/v1/voice/session",
        json={"device_id": reg["device_id"], "entry_point": "main"},
        headers=fx.device_headers(reg["device_id"], reg["credential_secret"], _nonce()),
    )
    assert after.status_code == 401
    assert after.json()["code"] == 40103
    # 活动 session 终止事件入库
    with fx.store.connect() as conn:
        terminated = conn.execute(
            "SELECT COUNT(*) FROM session_events"
            " WHERE device_id=? AND event_type='terminated'",
            (reg["device_id"],),
        ).fetchone()[0]
    assert terminated >= 1


def test_revoke_idempotent(fx: _Fixture) -> None:
    code, _ = _create_pairing(fx)
    reg = _register(fx, code)
    first = fx.client.post(
        f"/api/v1/voice/devices/{reg['device_id']}/revoke",
        json={"reason": "lost"},
        headers=fx.owner_headers(_nonce()),
    )
    assert first.status_code == 200
    second = fx.client.post(
        f"/api/v1/voice/devices/{reg['device_id']}/revoke",
        json={"reason": "lost again"},
        headers=fx.owner_headers(_nonce()),
    )
    assert second.status_code == 200
    assert second.json()["data"]["status"] == "revoked"
    with fx.store.connect() as conn:
        sessions = conn.execute(
            "SELECT COUNT(*) FROM revoked_sessions WHERE device_id=?", (reg["device_id"],)
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM device_credentials WHERE device_id=?", (reg["device_id"],)
        ).fetchone()[0]
    assert status == "revoked"
    assert sessions == 0  # 无活动 session → 幂等撤销不重复登记


def test_revoke_other_devices_unaffected(fx: _Fixture) -> None:
    code_a, _ = _create_pairing(fx)
    reg_a = _register(fx, code_a, device_name="phone-a")
    code_b, _ = _create_pairing(fx)
    reg_b = _register(fx, code_b, device_name="phone-b")
    fx.client.post(
        f"/api/v1/voice/devices/{reg_a['device_id']}/revoke",
        json={"reason": "lost"},
        headers=fx.owner_headers(_nonce()),
    )
    session_b = fx.client.post(
        "/api/v1/voice/session",
        json={"device_id": reg_b["device_id"], "entry_point": "main"},
        headers=fx.device_headers(reg_b["device_id"], reg_b["credential_secret"], _nonce()),
    )
    assert session_b.status_code == 201
    assert session_b.json()["data"]["user_id"] == reg_b["device_id"]


def test_revoke_unknown_device_404(fx: _Fixture) -> None:
    resp = fx.client.post(
        f"/api/v1/voice/devices/{uuid.uuid4()}/revoke",
        json={"reason": "x"},
        headers=fx.owner_headers(_nonce()),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 40401


def test_revoke_failure_is_explicit_and_retryable(fx: _Fixture, monkeypatch) -> None:
    """跨进程终止失败：不得报告虚假成功，必须返回明确失败并保持可重试"""
    code, _ = _create_pairing(fx)
    reg = _register(fx, code)
    fx.client.post(
        "/api/v1/voice/session",
        json={"device_id": reg["device_id"], "entry_point": "main"},
        headers=fx.device_headers(reg["device_id"], reg["credential_secret"], _nonce()),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("sidecar termination channel unavailable")

    monkeypatch.setattr(fx.devices, "terminate_device_sessions", _boom)
    first = fx.client.post(
        f"/api/v1/voice/devices/{reg['device_id']}/revoke",
        json={"reason": "lost"},
        headers=fx.owner_headers(_nonce()),
    )
    # 明确失败而非虚假成功；撤销主事务已完成（credential 已失效）
    # 2026-08-13：终止未确认迁移至 50301（termination_unconfirmed），50401 仅保留上游超时语义
    assert first.status_code == 503
    assert first.json()["code"] == 50301
    with fx.store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM device_credentials WHERE device_id=?", (reg["device_id"],)
        ).fetchone()[0]
    assert status == "revoked"
    # 重试（外部通道恢复）→ 幂等成功
    monkeypatch.undo()
    second = fx.client.post(
        f"/api/v1/voice/devices/{reg['device_id']}/revoke",
        json={"reason": "lost"},
        headers=fx.owner_headers(_nonce()),
    )
    assert second.status_code == 200
    assert second.json()["data"]["status"] == "revoked"
