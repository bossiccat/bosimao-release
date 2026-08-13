"""legacy 半双工网关生产 fail-closed 验收测试

生产组合（secured router only）：POST /api/v1/voice/pair 与 WS /ws/voice 不可达，
/api/v1/voice/status 为安全版（无 Bearer → 40101）。
开发组合（legacy gateway 保留）：/pair 可达（开发联调保留，非生产）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import build_voice_gateway, create_secured_voice_router
from app.voice.auth import CredentialValidator
from app.voice.config import ProductionGateError, VoiceSecurityConfig, production_gate
from app.voice.devices import DeviceService
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore

OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"


class _ConfirmingRtcTerminator:
    """测试装配中的可用 RTC 终止边界；只返回已确认退出的 session。"""

    def terminate_and_wait(self, device_id: str, session_ids: list[str]) -> list[str]:
        return list(session_ids)


def _production_app(tmp_path: Path) -> TestClient:
    """生产组合：只注册 secured router（VOICE_PRODUCTION=true 的等价装配）"""
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    security = VoiceSecurityConfig(
        production=True,
        tls_enabled=True,
        owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
        sidecar_credential_hash=CredentialValidator.hash_credential(SIDECAR_SECRET),
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=FAKE_SDK_APP_ID,
        trtc_secret_key=FAKE_SECRET_KEY,
        rtc_termination_enabled=True,
    )
    validator = CredentialValidator(store, security.owner_credential_hash,
                                    security.sidecar_credential_hash)
    app = FastAPI()
    app.include_router(
        create_secured_voice_router(
            store=store,
            service=RtcSessionService(
                RtcSessionConfig(sdk_app_id=FAKE_SDK_APP_ID, secret_key=FAKE_SECRET_KEY,
                                 room_prefix="jax-")
            ),
            validator=validator,
            nonces=NonceService(store),
            limiter=RateLimiter(store, RateLimitConfig()),
            security=security,
            devices=DeviceService(store, terminator=_ConfirmingRtcTerminator()),
        )
    )
    return TestClient(app)


def _dev_app(tmp_path: Path) -> TestClient:
    """开发组合：保留 legacy gateway（main.py 在非生产模式注册）"""
    router, _manager = build_voice_gateway()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_production_requires_rtc_termination_capability() -> None:
    security = VoiceSecurityConfig(
        production=True,
        tls_enabled=True,
        owner_credential_hash="configured",
        sidecar_credential_hash="configured",
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=FAKE_SDK_APP_ID,
        trtc_secret_key=FAKE_SECRET_KEY,
        rtc_termination_enabled=False,
    )
    with pytest.raises(ProductionGateError):
        production_gate(security)


def test_production_app_has_no_legacy_pair_endpoint(tmp_path: Path) -> None:
    client = _production_app(tmp_path)
    resp = client.post("/api/v1/voice/pair")
    assert resp.status_code == 404  # 生产模式不注册匿名配对旁路


def test_production_app_ws_voice_unreachable(tmp_path: Path) -> None:
    client = _production_app(tmp_path)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/voice"):
            pass


def test_production_app_status_is_secured(tmp_path: Path) -> None:
    client = _production_app(tmp_path)
    resp = client.get("/api/v1/voice/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101  # 生产 status 需要 Bearer，非匿名


def test_dev_app_keeps_legacy_pair_endpoint(tmp_path: Path) -> None:
    client = _dev_app(tmp_path)
    resp = client.post("/api/v1/voice/pair")
    assert resp.status_code == 200  # 开发联调保留（非生产，不作为交付证据）
