"""QA 独立验收测试：POST /api/v1/voice/session userSig 契约（反自证）

状态：**QA 独立测试（qa，2026-08-06），不依赖 be-pc 的 parse_user_sig 自证**。
- 独立实现 TRTC 官方 TLSSigAPIv2 算法验签器（zlib.compress + 自定义 base64：
  +→*、/→-、=→_；HMAC-SHA256；TLS.time/TLS.expire 字段）。
- 不 import app.voice.usersig 的任何函数（只 import RtcSessionService 注入假 SecretKey 以调用接口）。
- 断言口径来自 ADR-012 / PC-INTEGRATION §2.3 契约。
- 用**假 SecretKey**（QA 常量，禁止真实 .env 密钥值出现）。

⚠️ 2026-08-06 契约修正（架构师裁决，be-pc 落实）：
  - 手机 userId = 请求 device_id（**原 pc-phone 定值废弃**）；
  - userSig 的 TLS.identifier 与响应 user_id 均等于 device_id；
  - 本测试断言口径已同步（identifier 参数按传入 device_id 校验）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import zlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice import build_session_router
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService

# 假 SecretKey（QA 常量，禁止使用 .env 真实密钥）
FAKE_SECRET_KEY = "qa-independent-fake-secret-key-0123456789abcdef"
FAKE_SDK_APP_ID = 1600155678
ROOM_PREFIX = "jax-"
# 契约（架构师裁决 2026-08-06）：user_id == device_id；pc-phone 定值废弃
DEVICE_ID = "dev-001"


# ---------- 独立验签器（官方 TLSSigAPIv2 算法，不 import usersig 自证） ----------

def _b64_decode_url(data: str) -> bytes:
    """官方 base64_decode_url：*→+、-→/、_→="""
    s = data.replace("*", "+").replace("-", "/").replace("_", "=")
    return base64.b64decode(s)


def parse_user_sig_independent(user_sig: str) -> dict:
    """独立解包 UserSig（zlib 解压 + json），验证基础结构"""
    try:
        raw = zlib.decompress(_b64_decode_url(user_sig))
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"userSig 解包失败（非 zlib+自定义base64 官方结构）: {e}") from e


def verify_user_sig_independent(user_sig: str, secret_key: str, identifier: str,
                                sdk_app_id: int, max_expire_s: int = 600,
                                now: int | None = None) -> dict:
    """独立验签（反自证核心）：HMAC-SHA256 重算比对 + 有效期校验"""
    now = now or int(time.time())
    payload = parse_user_sig_independent(user_sig)

    assert payload.get("TLS.ver") == "2.0", "TLS.ver 必须为 2.0"
    assert payload.get("TLS.identifier") == identifier, (
        f"identifier 不匹配: {payload.get('TLS.identifier')} != {identifier}"
    )
    assert int(payload.get("TLS.sdkappid")) == sdk_app_id, (
        f"sdkappid 不匹配: {payload.get('TLS.sdkappid')} != {sdk_app_id}"
    )
    expire = int(payload.get("TLS.expire", 0))
    assert expire > 0, "TLS.expire 必须 > 0"
    assert expire <= max_expire_s, f"userSig 有效期 {expire}s 超过契约上限 {max_expire_s}s"
    issued = int(payload.get("TLS.time", 0))
    assert issued <= now + 60, "TLS.time 不能在未来 60s 之外（时钟偏差兜底）"

    # 独立重算官方签名原文：TLS.identifier/sdkappid/time/expire 四行
    raw_to_sign = (
        f"TLS.identifier:{identifier}\n"
        f"TLS.sdkappid:{int(sdk_app_id)}\n"
        f"TLS.time:{issued}\n"
        f"TLS.expire:{expire}\n"
    )
    expect_sig = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), raw_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    assert payload.get("TLS.sig") == expect_sig, (
        "HMAC-SHA256 签名不匹配：userSig 非用给定 SecretKey/官方算法签发"
    )
    return payload


# ---------- 夹具 ----------

def _service(secret: str = FAKE_SECRET_KEY, sdk_app_id: int = FAKE_SDK_APP_ID) -> RtcSessionService:
    return RtcSessionService(
        RtcSessionConfig(
            sdk_app_id=sdk_app_id,
            secret_key=secret,
            room_prefix=ROOM_PREFIX,
            user_sig_expire_s=600,
        )
    )


def _client(service: RtcSessionService) -> TestClient:
    app = FastAPI()
    app.include_router(build_session_router(service))
    return TestClient(app)


def _post(client: TestClient, device_id: str) -> tuple[int, dict]:
    resp = client.post("/api/v1/voice/session", json={"device_id": device_id})
    return resp.status_code, resp.json()


# ---------- 用例：独立验签 ----------

def test_usersig_independent_verify_hmac_sha256():
    """官方算法正确性：独立验签器重算 HMAC-SHA256 通过（不依赖实现自证）"""
    svc = _service()
    data = svc.issue(DEVICE_ID)
    verify_user_sig_independent(data["user_sig"], FAKE_SECRET_KEY, DEVICE_ID, FAKE_SDK_APP_ID)


def test_usersig_expire_within_600s():
    """有效期 ≤ 600s（契约：短时效，ADR-012/PC-INTEGRATION §2.3）"""
    svc = _service()
    data = svc.issue(DEVICE_ID)
    payload = parse_user_sig_independent(data["user_sig"])
    assert 0 < int(payload["TLS.expire"]) <= 600


def test_usersig_identifier_is_device_id():
    """userId 必须等于请求 device_id（架构师裁决 2026-08-06；pc-phone 定值废弃）"""
    svc = _service()
    data = svc.issue(DEVICE_ID)
    payload = parse_user_sig_independent(data["user_sig"])
    assert payload["TLS.identifier"] == DEVICE_ID
    assert data["user_id"] == DEVICE_ID


def test_usersig_rejects_wrong_key():
    """独立验签反自证：错误 SecretKey 必须验签失败"""
    svc = _service()
    data = svc.issue(DEVICE_ID)
    with pytest.raises(AssertionError):
        verify_user_sig_independent(data["user_sig"], "wrong-key", DEVICE_ID, FAKE_SDK_APP_ID)


def test_usersig_rejects_tampered():
    """独立验签反自证：篡改 userSig 必须验签失败"""
    svc = _service()
    data = svc.issue(DEVICE_ID)
    tampered = data["user_sig"][:-4] + "AAAA"
    with pytest.raises(AssertionError):
        verify_user_sig_independent(tampered, FAKE_SECRET_KEY, DEVICE_ID, FAKE_SDK_APP_ID)


def test_usersig_device_id_mutual_exclusion():
    """独立验签反自证（越权防线）：A device 的 userSig 用 B device 验签必须失败。

    防实现把所有 device 签成同一 identifier（一旦发生，任意手机可冒用他人 userId 进房）。
    """
    svc = _service()
    data_a = svc.issue("dev-alpha")
    data_b = svc.issue("dev-beta")
    # 各自用正确 device 验签通过
    verify_user_sig_independent(data_a["user_sig"], FAKE_SECRET_KEY, "dev-alpha", FAKE_SDK_APP_ID)
    verify_user_sig_independent(data_b["user_sig"], FAKE_SECRET_KEY, "dev-beta", FAKE_SDK_APP_ID)
    # 交叉验签必须失败（identifier 不匹配）
    with pytest.raises(AssertionError):
        verify_user_sig_independent(data_a["user_sig"], FAKE_SECRET_KEY, "dev-beta", FAKE_SDK_APP_ID)
    with pytest.raises(AssertionError):
        verify_user_sig_independent(data_b["user_sig"], FAKE_SECRET_KEY, "dev-alpha", FAKE_SDK_APP_ID)


# ---------- 用例：路由契约 ----------

def test_session_returns_contract_fields():
    """契约（OpenAPI SessionResponse）：HTTP 201 + room_id/user_id/user_sig/sdk_app_id/scene/session_id/expires_at；user_id==device_id"""
    client = _client(_service())
    status, body = _post(client, DEVICE_ID)
    assert status == 201
    data = body["data"]
    assert data["room_id"].startswith(ROOM_PREFIX)
    assert data["user_id"] == DEVICE_ID
    assert data["user_sig"]
    assert data["sdk_app_id"] == FAKE_SDK_APP_ID
    assert data["scene"] == "trtc_full_duplex"
    assert data["session_id"]
    assert data["expires_at"]


def test_same_device_idempotent_room():
    """同 device 幂等：room_id 确定性复用（无内存状态，纯派生）"""
    client = _client(_service())
    _, p1 = _post(client, "same-dev")
    _, p2 = _post(client, "same-dev")
    assert p1["data"]["room_id"] == p2["data"]["room_id"]


def test_different_device_different_room():
    """不同 device 房间隔离"""
    client = _client(_service())
    _, p1 = _post(client, "dev-a")
    _, p2 = _post(client, "dev-b")
    assert p1["data"]["room_id"] != p2["data"]["room_id"]


def test_missing_device_id_422():
    """缺 device_id → 422（Pydantic 校验）"""
    client = _client(_service())
    resp = client.post("/api/v1/voice/session", json={})
    assert resp.status_code == 422


def test_invalid_device_id_400():
    """非法 device_id（含非法字符）→ 400"""
    client = _client(_service())
    resp = client.post("/api/v1/voice/session", json={"device_id": "bad id!@#"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_config_missing_503():
    """TRTC 凭据未配置 → 503（SDKAppID/SecretKey 缺失时不产生假凭证）"""
    client = _client(_service(secret="", sdk_app_id=0))
    resp = client.post("/api/v1/voice/session", json={"device_id": "dev-001"})
    assert resp.status_code == 503
    assert resp.json()["code"] == 50300


def test_secret_key_not_in_response():
    """反泄露：响应体不得含 SecretKey（假 key 也不允许出现）"""
    client = _client(_service())
    _, body = _post(client, "dev-001")
    body_str = json.dumps(body, ensure_ascii=False)
    assert FAKE_SECRET_KEY not in body_str


def test_usersig_not_contain_secret_key():
    """userSig 内不得出现 SecretKey 明文（签名值≠密钥值）"""
    client = _client(_service())
    _, body = _post(client, "dev-001")
    assert FAKE_SECRET_KEY not in body["data"]["user_sig"]


# ---------- 独立验签器自检（验证测试工具本身正确，始终执行） ----------

def test_self_check_independent_verifier_rejects_wrong_sig():
    """测试工具自检：用错误密钥构造的 userSig 必须被独立验签器拒绝（防验签器形同虚设）"""
    import time as _t

    issued = int(_t.time())
    raw_to_sign = (
        f"TLS.identifier:{DEVICE_ID}\n"
        f"TLS.sdkappid:{FAKE_SDK_APP_ID}\n"
        f"TLS.time:{issued}\n"
        f"TLS.expire:{600}\n"
    )
    sig = base64.b64encode(
        hmac.new(b"wrong", raw_to_sign.encode(), hashlib.sha256).digest()
    ).decode("ascii")
    payload = {
        "TLS.ver": "2.0", "TLS.identifier": DEVICE_ID,
        "TLS.sdkappid": FAKE_SDK_APP_ID, "TLS.expire": 600,
        "TLS.time": issued, "TLS.sig": sig,
    }
    bad_sig = _b64_encode_url(zlib.compress(json.dumps(payload).encode()))
    with pytest.raises(AssertionError):
        verify_user_sig_independent(bad_sig, FAKE_SECRET_KEY, DEVICE_ID, FAKE_SDK_APP_ID)


def _b64_encode_url(data: bytes) -> str:
    s = base64.b64encode(data).decode("ascii")
    return s.replace("+", "*").replace("/", "-").replace("=", "_")
