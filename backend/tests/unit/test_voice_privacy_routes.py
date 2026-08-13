"""隐私开关路由 + 真实 RuntimeActions 验收测试（ADR-021 D1/D2/D3/D4）

- GET  /api/v1/privacy：owner/device/sidecar 任一主体可读全部开关
- PATCH /api/v1/privacy/{setting}：owner only + nonce + 限流，写审计
- cloud_processing_enabled=false → voice/session 签发返回 40301（fail-closed）
- PrivacyRuntimeActions：desktop_capture 走 late-bound orchestrator，bind 前 apply 失败回滚
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice_secured import create_secured_voice_router
from app.core.orchestrator import Orchestrator
from app.voice.auth import CredentialValidator
from app.voice.config import VoiceSecurityConfig
from app.voice.nonce import NonceService
from app.voice.privacy import (
    FakeRuntimeActions,
    PrivacyRuntimeActions,
    PrivacyService,
    privacy_runtime,
)
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore

OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
DEVICE_A = "dev-a-000000000000000000000001"
SECRET_A = "secret-a-0123456789abcdef01234567"
FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"


def _security() -> VoiceSecurityConfig:
    # 非生产（dev）：豁免 tls 但保留 owner/sidecar/nonce/限流/TRTC 全部能力项
    return VoiceSecurityConfig(
        production=False,
        tls_enabled=False,
        owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
        sidecar_credential_hash=CredentialValidator.hash_credential(SIDECAR_SECRET),
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=FAKE_SDK_APP_ID,
        trtc_secret_key=FAKE_SECRET_KEY,
    )


def _build(tmp_path: Path, privacy: PrivacyService | None = None):
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE_A, SECRET_A, device_name="phone-a")
    validator = CredentialValidator(
        store=store,
        owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
        sidecar_credential_hash=CredentialValidator.hash_credential(SIDECAR_SECRET),
    )
    if privacy is None:
        privacy = PrivacyService(store, FakeRuntimeActions())
    service = RtcSessionService(
        RtcSessionConfig(sdk_app_id=FAKE_SDK_APP_ID, secret_key=FAKE_SECRET_KEY,
                         room_prefix="jax-")
    )
    router = create_secured_voice_router(
        store=store,
        service=service,
        validator=validator,
        nonces=NonceService(store),
        limiter=RateLimiter(store, RateLimitConfig()),
        security=_security(),
        privacy=privacy,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), store


def _owner_headers(nonce: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {OWNER_SECRET}"}
    if nonce is not None:
        headers["X-Request-Nonce"] = nonce
    return headers


# ---------- GET /api/v1/privacy ----------

def test_get_privacy_owner_reads_all_settings(tmp_path: Path) -> None:
    client, _store = _build(tmp_path)
    resp = client.get("/api/v1/privacy", headers=_owner_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["settings"] == {
        "cloud_processing_enabled": True,
        "microphone_enabled": True,
        "background_conversation_enabled": True,
        "desktop_capture_enabled": True,
        "transcript_persistence_enabled": False,
    }


@pytest.mark.parametrize("token", [f"{DEVICE_A}.{SECRET_A}", SIDECAR_SECRET])
def test_get_privacy_device_and_sidecar_can_read(tmp_path: Path, token: str) -> None:
    client, _store = _build(tmp_path)
    resp = client.get("/api/v1/privacy", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


def test_get_privacy_requires_auth(tmp_path: Path) -> None:
    client, _store = _build(tmp_path)
    resp = client.get("/api/v1/privacy")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


# ---------- PATCH /api/v1/privacy/{setting} ----------

def test_patch_privacy_owner_toggles_and_persists(tmp_path: Path) -> None:
    client, store = _build(tmp_path)
    resp = client.patch(
        "/api/v1/privacy/microphone",
        json={"enabled": False},
        headers=_owner_headers(uuid.uuid4().hex),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["setting"] == "microphone_enabled"
    assert data["effective_value"] is False
    assert data["action_result"] == "ok"
    assert data["applied_at"] > 0
    # 持久化：新 service 实例读到 False
    assert PrivacyService(store, FakeRuntimeActions()).get("microphone_enabled") is False


def test_patch_privacy_writes_audit_with_old_new_actor(tmp_path: Path) -> None:
    client, store = _build(tmp_path)
    client.patch(
        "/api/v1/privacy/desktop_capture",
        json={"enabled": False},
        headers=_owner_headers(uuid.uuid4().hex),
    )
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT action, subject_type, subject_id, result, metadata_redacted_json"
            " FROM privacy_audit_events WHERE action='privacy.toggle'"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["subject_type"] == "setting"
    assert row["subject_id"] == "desktop_capture_enabled"
    assert row["result"] == "ok"
    assert json.loads(row["metadata_redacted_json"]) == {
        "old": True, "new": False, "actor": "owner",
    }


def test_patch_privacy_rejects_non_owner(tmp_path: Path) -> None:
    client, _store = _build(tmp_path)
    resp = client.patch(
        "/api/v1/privacy/microphone",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}",
                 "X-Request-Nonce": uuid.uuid4().hex},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_patch_privacy_unknown_setting_400(tmp_path: Path) -> None:
    client, _store = _build(tmp_path)
    resp = client.patch(
        "/api/v1/privacy/not_a_thing",
        json={"enabled": False},
        headers=_owner_headers(uuid.uuid4().hex),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_patch_privacy_nonce_replay_401(tmp_path: Path) -> None:
    client, _store = _build(tmp_path)
    nonce = uuid.uuid4().hex
    r1 = client.patch("/api/v1/privacy/microphone", json={"enabled": False},
                      headers=_owner_headers(nonce))
    assert r1.status_code == 200
    r2 = client.patch("/api/v1/privacy/microphone", json={"enabled": False},
                      headers=_owner_headers(nonce))
    assert r2.status_code == 401
    assert r2.json()["code"] == 40102


# ---------- cloud 门禁（fail-closed） ----------

def test_voice_session_cloud_disabled_returns_40301(tmp_path: Path) -> None:
    client, store = _build(tmp_path)
    PrivacyService(store, FakeRuntimeActions()).set("cloud_processing_enabled", False)
    resp = client.post(
        "/api/v1/voice/session",
        json={"device_id": DEVICE_A, "entry_point": "main"},
        headers={"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}",
                 "X-Request-Nonce": uuid.uuid4().hex},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 40301


def test_voice_session_cloud_enabled_still_issues(tmp_path: Path) -> None:
    client, _store = _build(tmp_path)
    resp = client.post(
        "/api/v1/voice/session",
        json={"device_id": DEVICE_A, "entry_point": "main"},
        headers={"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}",
                 "X-Request-Nonce": uuid.uuid4().hex},
    )
    assert resp.status_code == 201
    assert resp.json()["code"] == 0


# ---------- PrivacyRuntimeActions（late-bound orchestrator） ----------

class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_desktop_capture(self, enabled: bool) -> None:
        self.calls.append(enabled)


def test_runtime_actions_desktop_apply_via_bound_orchestrator(tmp_path: Path) -> None:
    fake = _FakeOrchestrator()
    old = privacy_runtime.get()
    privacy_runtime.bind(fake)
    try:
        store = VoiceStore(tmp_path / "voice.db")
        store.initialize()
        svc = PrivacyService(store, PrivacyRuntimeActions())
        result = svc.set("desktop_capture_enabled", False)
        assert result["action_result"] == "ok"
        assert fake.calls == [False]
    finally:
        privacy_runtime.bind(old)


def test_runtime_actions_desktop_bind_before_apply_fails(tmp_path: Path) -> None:
    old = privacy_runtime.get()
    privacy_runtime.bind(None)
    try:
        store = VoiceStore(tmp_path / "voice.db")
        store.initialize()
        svc = PrivacyService(store, PrivacyRuntimeActions())
        result = svc.set("desktop_capture_enabled", False)
        assert result["action_result"] == "failed"
        assert result["effective_value"] is True  # 回滚为原值
        assert svc.get("desktop_capture_enabled") is True
    finally:
        privacy_runtime.bind(old)


def test_runtime_actions_cloud_mic_background_noop(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    svc = PrivacyService(store, PrivacyRuntimeActions())
    for setting in ("cloud_processing_enabled", "microphone_enabled",
                    "background_conversation_enabled"):
        result = svc.set(setting, False)
        assert result["action_result"] == "ok"
        assert svc.get(setting) is False


# ---------- Orchestrator.set_desktop_capture（精确恢复红线） ----------

class _FakeTarget:
    def __init__(self, app_id: str) -> None:
        self.app_id = app_id


class _FakeSession:
    def __init__(self, app_id: str, authorized: bool, wgc) -> None:
        self.target = _FakeTarget(app_id)
        self.authorized = authorized
        self.wgc = wgc
        self.mode = "wgc" if wgc else "none"
        self.window = object()


class _FakeSessions:
    def __init__(self, sessions: list[_FakeSession]) -> None:
        self.sessions = sessions
        self.located = False
        self.stopped = False
        self.started: list[str] = []

    def all(self):
        return self.sessions

    def get(self, app_id: str):
        for s in self.sessions:
            if s.target.app_id == app_id:
                return s
        return None

    def locate_all(self) -> None:
        self.located = True
        for s in self.sessions:
            if s.window is not None:
                s.mode = "wgc" if s.authorized else "pending-auth"

    def stop_all(self) -> None:
        self.stopped = True
        for s in self.sessions:
            s.wgc = None
            s.mode = "none"

    def start_wgc(self, app_id: str) -> None:
        self.started.append(app_id)


def test_set_desktop_capture_stop_records_and_restores_exact_windows() -> None:
    orch = object.__new__(Orchestrator)
    active = _FakeSession("app-a", authorized=True, wgc=object())
    inactive_authorized = _FakeSession("app-b", authorized=True, wgc=None)
    unauthorized = _FakeSession("app-c", authorized=False, wgc=object())
    sessions = _FakeSessions([active, inactive_authorized, unauthorized])
    orch._sessions = sessions
    orch._monitor_enabled = True
    orch._desktop_capture_paused = set()

    orch.set_desktop_capture(False)
    assert orch._monitor_enabled is False
    assert sessions.stopped is True
    # 只记录「仍在捕获且已授权」的窗口（未授权/未在捕获的不入恢复集）
    assert orch._desktop_capture_paused == {"app-a"}

    orch.set_desktop_capture(True)
    assert orch._monitor_enabled is True
    assert sessions.located is True
    # 红线：只恢复停止时记录的窗口，未授权/新出现窗口绝不误恢复
    assert sessions.started == ["app-a"]
