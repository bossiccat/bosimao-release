"""RTC 会话签发单元测试（PC-INTEGRATION §2.3 契约；假 SecretKey，禁止真实密钥）

覆盖：
- GenUserSig（TLSSigAPIv2 官方算法）：生成不抛错、字段齐全、有效期 ≤600s、同输入确定性
- 会话签发：同 device 幂等复用 room_id、不同 device 隔离、scene/sdk_app_id 正确
- 契约（架构师裁决）：user_id == 请求 device_id（userSig identifier 同值）
- 路由：POST /api/v1/voice/session 成功/非法 device_id/凭据缺失
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import build_session_router
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.usersig import gen_user_sig, parse_user_sig

# 假 SecretKey（测试专用，禁止使用 .env 中的真实密钥）
FAKE_SECRET_KEY = "fake-secret-key-0123456789abcdef0123456789abcdef"
FAKE_SDK_APP_ID = 1600155678
FAKE_ROOM_PREFIX = "jax-"


def _service(secret: str = FAKE_SECRET_KEY, sdk_app_id: int = FAKE_SDK_APP_ID) -> RtcSessionService:
    return RtcSessionService(
        RtcSessionConfig(
            sdk_app_id=sdk_app_id,
            secret_key=secret,
            room_prefix=FAKE_ROOM_PREFIX,
            user_sig_expire_s=600,
        )
    )


def _client(service: RtcSessionService) -> TestClient:
    app = FastAPI()
    app.include_router(build_session_router(service))
    return TestClient(app)


# ---------- GenUserSig（TLSSigAPIv2 官方算法） ----------
def test_gen_user_sig_no_throw_and_fields():
    """生成不抛错；解码后字段齐全（TLS.ver/identifier/sdkappid/expire/time/sig）"""
    sig = gen_user_sig(FAKE_SDK_APP_ID, FAKE_SECRET_KEY, "dev-001", expire_s=600)
    assert isinstance(sig, str) and sig
    payload = parse_user_sig(sig)
    assert payload["TLS.ver"] == "2.0"
    assert payload["TLS.identifier"] == "dev-001"
    assert int(payload["TLS.sdkappid"]) == FAKE_SDK_APP_ID
    assert int(payload["TLS.expire"]) == 600
    assert int(payload["TLS.time"]) > 0
    assert payload["TLS.sig"]


def test_gen_user_sig_expire_within_600():
    """有效期字段 ≤600s（契约：userSig 短时效）"""
    sig = gen_user_sig(FAKE_SDK_APP_ID, FAKE_SECRET_KEY, "dev-001", expire_s=600)
    payload = parse_user_sig(sig)
    assert int(payload["TLS.expire"]) <= 600


def test_gen_user_sig_deterministic():
    """同输入（time 相同）→ 同 sig（HMAC-SHA256 确定性）"""
    # monkeypatch 固定 time 以便比对
    import app.voice.usersig as usersig

    fixed = 1_700_000_000
    original = usersig.time.time
    usersig.time.time = lambda: fixed
    try:
        a = gen_user_sig(FAKE_SDK_APP_ID, FAKE_SECRET_KEY, "dev-001", expire_s=600)
        b = gen_user_sig(FAKE_SDK_APP_ID, FAKE_SECRET_KEY, "dev-001", expire_s=600)
    finally:
        usersig.time.time = original
    assert a == b


def test_gen_user_sig_compressed_base64_url_alphabet():
    """userSig 是 zlib 压缩 + 自定义 base64（+→* /→- =→_），可正常解压解码"""
    sig = gen_user_sig(FAKE_SDK_APP_ID, FAKE_SECRET_KEY, "dev-001", expire_s=600)
    # 自定义 alphabet：不应出现标准 base64 的 +/= 字符
    assert "+" not in sig and "=" not in sig
    payload = parse_user_sig(sig)
    assert payload["TLS.sig"]


def test_gen_user_sig_missing_key_raises():
    """SecretKey 为空应抛错（防止未配置时静默生成无效签名）"""
    with pytest.raises(ValueError):
        gen_user_sig(FAKE_SDK_APP_ID, "", "dev-001", expire_s=600)


# ---------- 会话签发服务 ----------
def test_issue_same_device_idempotent_room():
    """同 device 幂等：重复签发返回同一 room_id（jax-<device_id>）"""
    svc = _service()
    r1 = svc.issue("dev-001")
    r2 = svc.issue("dev-001")
    assert r1["room_id"] == "jax-dev-001"
    assert r2["room_id"] == r1["room_id"]
    assert r1["scene"] == "audio_call"
    assert int(r1["sdk_app_id"]) == FAKE_SDK_APP_ID
    # userSig 短时效且可解析
    assert int(parse_user_sig(r1["user_sig"])["TLS.expire"]) <= 600


def test_issue_user_id_equals_device_id():
    """契约：user_id == 请求 device_id；userSig 的 TLS.identifier 同值（废弃 pc-phone 定值）"""
    svc = _service()
    for device in ("dev-001", "dev-abc", "device_9"):
        r = svc.issue(device)
        assert r["user_id"] == device
        assert parse_user_sig(r["user_sig"])["TLS.identifier"] == device


def test_issue_different_device_different_room_and_user():
    svc = _service()
    a = svc.issue("dev-a")
    b = svc.issue("dev-b")
    assert a["room_id"] != b["room_id"]
    assert a["user_id"] == "dev-a"
    assert b["user_id"] == "dev-b"


def test_issue_invalid_device_id():
    svc = _service()
    with pytest.raises(Exception) as ei:
        svc.issue("非法/设备?")
    assert type(ei.value).__name__ == "InvalidDeviceIdError"
    with pytest.raises(Exception):
        svc.issue("")


def test_issue_config_missing():
    """凭据缺失（空 SecretKey）→ ConfigMissingError"""
    svc = _service(secret="")
    with pytest.raises(Exception) as ei:
        svc.issue("dev-001")
    assert type(ei.value).__name__ == "ConfigMissingError"


# ---------- 路由：POST /api/v1/voice/session ----------
def test_route_session_success():
    client = _client(_service())
    resp = client.post("/api/v1/voice/session", json={"device_id": "dev-001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["room_id"] == "jax-dev-001"
    assert data["user_id"] == "dev-001"
    assert data["scene"] == "audio_call"
    assert int(data["sdk_app_id"]) == FAKE_SDK_APP_ID
    # userSig 签给 device_id（手机用自己的 device_id 进房）
    assert parse_user_sig(data["user_sig"])["TLS.identifier"] == "dev-001"


def test_route_session_user_id_matches_device():
    """路由层：多 device 请求，user_id 均等于各自 device_id"""
    client = _client(_service())
    for device in ("dev-a", "dev-b", "dev-c"):
        data = client.post("/api/v1/voice/session", json={"device_id": device}).json()["data"]
        assert data["user_id"] == device


def test_route_session_idempotent():
    client = _client(_service())
    r1 = client.post("/api/v1/voice/session", json={"device_id": "dev-001"}).json()["data"]
    r2 = client.post("/api/v1/voice/session", json={"device_id": "dev-001"}).json()["data"]
    assert r1["room_id"] == r2["room_id"] == "jax-dev-001"
    assert r1["user_id"] == r2["user_id"] == "dev-001"


def test_route_session_invalid_device_id_400():
    client = _client(_service())
    resp = client.post("/api/v1/voice/session", json={"device_id": "非法!id"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_route_session_missing_credentials_503():
    client = _client(_service(secret=""))
    resp = client.post("/api/v1/voice/session", json={"device_id": "dev-001"})
    assert resp.status_code == 503
    assert resp.json()["code"] == 50300
