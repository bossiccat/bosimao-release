"""trtc-sign 云函数单测（假 key 验签，不触网）

覆盖：契约字段 / 假 key 验签（parse_user_sig 解包）/ 同 device 幂等 / userSig 有效期 ≤600s /
非法 device / 凭据缺失 / pending+sign 意图流 / CORS 预检 / 路由 404/405。
"""
from __future__ import annotations

import json

import pytest

from config import TrtcSignConfig
from index import main_handler
from signing import TrtcSignService
from usersig import parse_user_sig

FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"
ROOM_PREFIX = "jax-"


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    """所有用例默认注入假凭据到 os.environ，并复位 index 服务缓存"""
    monkeypatch.setenv("TRTC_SDKAPPID", str(FAKE_SDK_APP_ID))
    monkeypatch.setenv("TRTC_SECRETKEY", FAKE_SECRET_KEY)
    monkeypatch.setenv("TRTC_ROOM_PREFIX", ROOM_PREFIX)
    import index as _idx

    _idx._svc = None
    yield
    _idx._svc = None


def _env(secret: str = FAKE_SECRET_KEY, sdk_app_id: int = FAKE_SDK_APP_ID, **extra) -> dict:
    env = {
        "TRTC_SDKAPPID": str(sdk_app_id),
        "TRTC_SECRETKEY": secret,
        "TRTC_ROOM_PREFIX": ROOM_PREFIX,
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _service(env: dict | None = None) -> TrtcSignService:
    return TrtcSignService(TrtcSignConfig(env or _env()))


def _event(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else "",
        "isBase64Encoded": False,
    }


# ---------- 契约字段 + 假 key 验签 ----------
def test_issue_contract_fields_and_signature():
    svc = _service()
    data = svc.issue("jax-dev-001")
    assert data["room_id"] == "jax-jax-dev-001"
    assert data["user_id"] == "jax-dev-001"
    assert data["scene"] == "audio_call"
    assert data["sdk_app_id"] == FAKE_SDK_APP_ID
    # 假 key 验签：解包 userSig，identifier 必须是 device_id，expire 必须 ≤600
    payload = parse_user_sig(data["user_sig"])
    assert payload["TLS.identifier"] == "jax-dev-001"
    assert int(payload["TLS.sdkappid"]) == FAKE_SDK_APP_ID
    assert int(payload["TLS.expire"]) <= 600


def test_issue_sig_deterministic_with_fixed_time(monkeypatch):
    svc = _service()
    monkeypatch.setattr("usersig.time.time", lambda: 1_700_000_000)
    a = svc.issue("dev-a")["user_sig"]
    b = svc.issue("dev-a")["user_sig"]
    assert a == b  # 同时间同 key 同 user_id → 字节级一致


# ---------- 同 device 幂等 ----------
def test_issue_idempotent_room():
    svc = _service()
    r1 = svc.issue("dev-001")
    r2 = svc.issue("dev-001")
    assert r1["room_id"] == r2["room_id"] == "jax-dev-001"
    assert r1["user_id"] == r2["user_id"] == "dev-001"


# ---------- userSig 有效期硬约束 ----------
def test_expire_capped_at_600():
    svc = _service(env=_env(TRTC_USER_SIG_EXPIRE_S="3600"))
    assert svc.cfg.expire_s == 600  # 超限回退 600
    data = svc.issue("dev-001")
    assert int(parse_user_sig(data["user_sig"])["TLS.expire"]) <= 600


# ---------- 非法输入 ----------
def test_issue_invalid_device():
    svc = _service()
    with pytest.raises(Exception) as ei:
        svc.issue("非法/设备?")
    assert type(ei.value).__name__ == "InvalidDeviceIdError"
    with pytest.raises(Exception):
        svc.issue("")


# ---------- 凭据缺失 ----------
def test_issue_config_missing():
    svc = _service(env=_env(secret=""))
    with pytest.raises(Exception) as ei:
        svc.issue("dev-001")
    assert type(ei.value).__name__ == "ConfigMissingError"


# ---------- HTTP 路由层 ----------
def test_route_session_success():
    resp = main_handler(_event("POST", "/api/v1/voice/session", {"device_id": "dev-001"}), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["code"] == 0
    assert body["data"]["room_id"] == "jax-dev-001"
    assert body["data"]["user_id"] == "dev-001"


def test_route_session_invalid_device_400():
    resp = main_handler(_event("POST", "/api/v1/voice/session", {"device_id": "非法!id"}), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["code"] == 40001


def test_route_session_missing_credentials_503(monkeypatch):
    monkeypatch.setenv("TRTC_SECRETKEY", "")
    resp = main_handler(_event("POST", "/api/v1/voice/session", {"device_id": "dev-001"}), None)
    assert resp["statusCode"] == 503
    assert json.loads(resp["body"])["code"] == 50300


# ---------- pending + sign 意图流（PC 协调） ----------
def test_pending_sign_flow():
    svc = _service()
    svc.issue("dev-001")
    intent = svc.pending("dev-001")
    assert intent is not None
    assert intent["room_id"] == "jax-dev-001"
    # PC 取自身 userSig
    pc = svc.sign_for_sidecar("dev-001", user_id="jax-pc-sidecar")
    assert pc["room_id"] == "jax-dev-001"
    assert pc["user_id"] == "jax-pc-sidecar"
    assert parse_user_sig(pc["user_sig"])["TLS.identifier"] == "jax-pc-sidecar"
    # 消费后 pending 为空
    assert svc.pending("dev-001") is None


def test_sign_without_issue_404():
    svc = _service()
    with pytest.raises(Exception) as ei:
        svc.sign_for_sidecar("never-issued", user_id="jax-pc-sidecar")
    assert type(ei.value).__name__ == "UnknownDeviceError"


def test_route_pending_and_sign():
    # 先 issue（写意图）
    main_handler(_event("POST", "/api/v1/voice/session", {"device_id": "dev-001"}), None)
    resp = main_handler(
        _event("GET", "/api/v1/voice/session/pending", query={"device_id": "dev-001"}), None
    )
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])["data"]
    assert data["room_id"] == "jax-dev-001"

    resp = main_handler(
        _event("POST", "/api/v1/voice/session/sign", {"device_id": "dev-001", "user_id": "jax-pc-sidecar"}),
        None,
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["data"]["user_id"] == "jax-pc-sidecar"
    assert body["data"]["room_id"] == "jax-dev-001"

    # 已消费 → pending 为空
    resp = main_handler(
        _event("GET", "/api/v1/voice/session/pending", query={"device_id": "dev-001"}), None
    )
    assert json.loads(resp["body"])["data"] is None


# ---------- CORS 预检 + 路由兜底 ----------
def test_options_preflight():
    resp = main_handler(_event("OPTIONS", "/api/v1/voice/session"), None)
    assert resp["statusCode"] == 204
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_route_unknown_404_and_method_405():
    resp = main_handler(_event("GET", "/api/v1/voice/unknown"), None)
    assert resp["statusCode"] == 404
    resp = main_handler(_event("PUT", "/api/v1/voice/session", {"device_id": "dev-001"}), None)
    assert resp["statusCode"] == 405
