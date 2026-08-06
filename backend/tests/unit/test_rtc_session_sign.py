"""POST /api/v1/voice/session/sign 路由单测（PC sidecar 进房签发，PC-INTEGRATION §2.3）

- 同一 device_id → 同一 room_id；userSig 签给 user_id（默认 jax-pc-sidecar）
- 非法 device_id / user_id → 400；凭据缺失 → 503
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import build_session_router
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.usersig import parse_user_sig

FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"


def _service(secret: str = FAKE_SECRET_KEY, sdk_app_id: int = FAKE_SDK_APP_ID) -> RtcSessionService:
    return RtcSessionService(
        RtcSessionConfig(sdk_app_id=sdk_app_id, secret_key=secret, room_prefix="jax-")
    )


def _client(service: RtcSessionService) -> TestClient:
    app = FastAPI()
    app.include_router(build_session_router(service))
    return TestClient(app)


def test_sign_sidecar_success():
    client = _client(_service())
    resp = client.post("/api/v1/voice/session/sign", json={"device_id": "dev-001", "user_id": "jax-pc-sidecar"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["room_id"] == "jax-dev-001"
    assert data["user_id"] == "jax-pc-sidecar"
    assert data["scene"] == "audio_call"
    assert int(data["sdk_app_id"]) == FAKE_SDK_APP_ID
    assert parse_user_sig(data["user_sig"])["TLS.identifier"] == "jax-pc-sidecar"


def test_sign_default_user_id_is_sidecar():
    client = _client(_service())
    resp = client.post("/api/v1/voice/session/sign", json={"device_id": "dev-001"})
    assert resp.status_code == 200
    assert resp.json()["data"]["user_id"] == "jax-pc-sidecar"


def test_sign_same_room_as_issue():
    """sign 与 issue 同一 device_id → 同一 room_id（手机与 PC sidecar 进同一房间）"""
    client = _client(_service())
    phone = client.post("/api/v1/voice/session", json={"device_id": "dev-001"}).json()["data"]
    pc = client.post(
        "/api/v1/voice/session/sign", json={"device_id": "dev-001", "user_id": "jax-pc-sidecar"}
    ).json()["data"]
    assert pc["room_id"] == phone["room_id"] == "jax-dev-001"
    assert pc["user_sig"] != phone["user_sig"]  # 不同 userId → 不同签名


def test_sign_invalid_device_400():
    client = _client(_service())
    resp = client.post(
        "/api/v1/voice/session/sign", json={"device_id": "非法!id", "user_id": "jax-pc-sidecar"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_sign_invalid_user_400():
    client = _client(_service())
    resp = client.post("/api/v1/voice/session/sign", json={"device_id": "dev-001", "user_id": "bad user!"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_sign_missing_credentials_503():
    client = _client(_service(secret=""))
    resp = client.post(
        "/api/v1/voice/session/sign", json={"device_id": "dev-001", "user_id": "jax-pc-sidecar"}
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == 50300
