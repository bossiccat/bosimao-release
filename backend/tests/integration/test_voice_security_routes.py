"""商业语音安全路由集成验收测试（QA spec §5.4）

真实 FastAPI router + 正式 auth/nonce/rate-limit/storage service + 临时 SQLite。
允许 fake TRTC SecretKey / 受控 clock，禁止把整个 auth/router/store mock 掉。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.voice.usersig import parse_user_sig
from .voice_security_fixture import (
    DEVICE_A,
    DEVICE_B,
    FAKE_SDK_APP_ID,
    SECRET_A,
    SECRET_B,
    SIDECAR_NEXT_SECRET,
    SIDECAR_SECRET,
    VoiceSecurityFixture,
    nonce,
)

_Fixture = VoiceSecurityFixture
_nonce = nonce


@pytest.fixture
def fx(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


# ---------- 1. /session 鉴权与签发 ----------

def test_session_without_bearer_returns_40101(fx: _Fixture) -> None:
    resp = fx.client.post("/api/v1/voice/session", json=fx.session_payload(DEVICE_A))
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 40101
    assert body["message"] == "auth_failed"


def test_session_valid_device_and_nonce_returns_201(fx: _Fixture) -> None:
    resp = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_A),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, _nonce()),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["scene"] == "trtc_full_duplex"
    assert data["session_id"]
    assert data["room_id"] == f"jax-{DEVICE_A}"
    assert data["user_id"] == DEVICE_A
    assert int(data["sdk_app_id"]) == FAKE_SDK_APP_ID
    assert parse_user_sig(data["user_sig"])["TLS.identifier"] == DEVICE_A
    assert int(parse_user_sig(data["user_sig"])["TLS.expire"]) <= 600
    assert data["expires_at"] > time.time()


def test_device_a_credential_cannot_request_device_b(fx: _Fixture) -> None:
    resp = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_B),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, _nonce()),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    with fx.store.connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM session_events WHERE device_id=?", (DEVICE_B,)
        ).fetchone()[0]
    assert events == 0


def test_revoked_device_rejected_with_40103(fx: _Fixture) -> None:
    fx.store.revoke_device(DEVICE_B, reason="lost")
    resp = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_B),
        headers=fx.auth_headers(DEVICE_B, SECRET_B, _nonce()),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40103


# ---------- 2. 主体隔离：device vs sidecar ----------

def test_device_credential_cannot_use_sidecar_sign(fx: _Fixture) -> None:
    claim = fx.create_pending_claim()
    resp = fx.client.post(
        "/api/v1/voice/session/sign",
        json=fx.sign_payload(claim),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, _nonce()),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_sidecar_credential_cannot_use_device_session(fx: _Fixture) -> None:
    headers = {"Authorization": f"Bearer {SIDECAR_SECRET}", "X-Request-Nonce": _nonce()}
    resp = fx.client.post(
        "/api/v1/voice/session", json=fx.session_payload(DEVICE_A), headers=headers
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_sidecar_sign_success(fx: _Fixture) -> None:
    claim = fx.create_pending_claim()
    headers = {"Authorization": f"Bearer {SIDECAR_SECRET}", "X-Request-Nonce": _nonce()}
    resp = fx.client.post(
        "/api/v1/voice/session/sign",
        json=fx.sign_payload(claim),
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["room_id"] == f"jax-{DEVICE_A}"
    assert data["user_id"] == "jax-pc-sidecar"
    assert parse_user_sig(data["user_sig"])["TLS.identifier"] == "jax-pc-sidecar"
    assert int(parse_user_sig(data["user_sig"])["TLS.expire"]) <= 600


def test_current_and_next_both_sign_with_unchanged_response(tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, rotating=True)
    responses = []
    for secret in (SIDECAR_SECRET, SIDECAR_NEXT_SECRET):
        claim = fx.create_pending_claim()
        response = fx.client.post(
            "/api/v1/voice/session/sign",
            json=fx.sign_payload(claim),
            headers={"Authorization": f"Bearer {secret}", "X-Request-Nonce": _nonce()},
        )
        assert response.status_code == 201
        responses.append(response.json()["data"])
    assert {response["room_id"] for response in responses} == {f"jax-{DEVICE_A}"}
    assert {response["user_id"] for response in responses} == {"jax-pc-sidecar"}


# ---------- 3. nonce 防重放 ----------

def test_nonce_replay_returns_40102(fx: _Fixture) -> None:
    nonce = _nonce()
    first = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_A),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce),
    )
    assert first.status_code == 201
    second = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_A),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce),
    )
    assert second.status_code == 401
    assert second.json()["code"] == 40102


def test_concurrent_same_nonce_exactly_one_succeeds(fx: _Fixture) -> None:
    nonce = _nonce()
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def request() -> None:
        barrier.wait()
        resp = fx.client.post(
            "/api/v1/voice/session",
            json=fx.session_payload(DEVICE_A),
            headers=fx.auth_headers(DEVICE_A, SECRET_A, nonce),
        )
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(201) == 1
    assert results.count(401) == 3


def test_missing_nonce_rejected(fx: _Fixture) -> None:
    resp = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_A),
        headers=fx.auth_headers(DEVICE_A, SECRET_A),
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40102


# ---------- 4. 限流 ----------

def test_device_rate_limit_returns_42901_with_retry_after(tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, device_limit=3, ip_limit=1000)
    for _ in range(3):
        resp = fx.client.post(
            "/api/v1/voice/session",
            json=fx.session_payload(DEVICE_A),
            headers=fx.auth_headers(DEVICE_A, SECRET_A, _nonce()),
        )
        assert resp.status_code == 201
    limited = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_A),
        headers=fx.auth_headers(DEVICE_A, SECRET_A, _nonce()),
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == 42901
    assert limited.headers.get("Retry-After") is not None
    assert int(limited.headers["Retry-After"]) > 0


def test_ip_rate_limit_cannot_be_bypassed_by_other_token(tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, device_limit=1000, ip_limit=2)
    for _ in range(2):
        resp = fx.client.post(
            "/api/v1/voice/session",
            json=fx.session_payload(DEVICE_A),
            headers=fx.auth_headers(DEVICE_A, SECRET_A, _nonce()),
        )
        assert resp.status_code == 201
    other = fx.client.post(
        "/api/v1/voice/session",
        json=fx.session_payload(DEVICE_B),
        headers=fx.auth_headers(DEVICE_B, SECRET_B, _nonce()),
    )
    assert other.status_code == 429
    assert other.json()["code"] == 42901


