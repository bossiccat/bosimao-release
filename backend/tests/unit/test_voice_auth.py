"""认证/防重放/限流/生产 fail-closed 单元验收测试（QA spec §5.3）

全部使用真实 SQLite VoiceStore；禁止 mock-only。
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

import pytest

from app.voice.auth import (
    AuthError,
    CredentialPrincipal,
    CredentialValidator,
)
from app.voice.config import (
    ProductionGateError,
    VoiceSecurityConfig,
    validate_production,
)
from app.voice.errors import (
    ERROR_MESSAGES,
    HTTP_STATUS,
    VoiceError,
    error_payload,
)
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.storage import VoiceStore
from app.voice.usersig import gen_user_sig, parse_user_sig, user_sig_expire_ok

DEVICE_A = "dev-a-000000000000000000000001"
DEVICE_B = "dev-b-000000000000000000000002"
SECRET_A = "secret-a-0123456789abcdef01234567"
SECRET_B = "secret-b-0123456789abcdef01234567"
OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"


def _store(tmp_path: Path) -> VoiceStore:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE_A, SECRET_A, device_name="phone-a")
    store.save_device(DEVICE_B, SECRET_B, device_name="phone-b")
    return store


def _validator(store: VoiceStore) -> CredentialValidator:
    return CredentialValidator(
        store=store,
        owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
        sidecar_credential_hash=CredentialValidator.hash_credential(SIDECAR_SECRET),
    )


def _full_security() -> VoiceSecurityConfig:
    return VoiceSecurityConfig(
        production=True,
        tls_enabled=True,
        owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
        sidecar_credential_hash=CredentialValidator.hash_credential(SIDECAR_SECRET),
        nonce_enabled=True,
        rate_limit_enabled=True,
        trtc_sdk_app_id=1600155678,
        trtc_secret_key="fake-secret-key-for-test-only-0123456789",
    )


# ---------- 1. CredentialPrincipal 与主体隔离 ----------

def test_credential_principal_fields_are_server_derived(tmp_path: Path) -> None:
    principal = CredentialValidator(_store(tmp_path), "", "").verify_device(
        f"{DEVICE_A}.{SECRET_A}"
    )
    assert principal.type == "device"
    assert principal.subject_id == DEVICE_A
    assert principal.credential_id


def test_principal_cross_matrix_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    validator = _validator(store)
    # owner / device / sidecar 三类主体交叉使用必须全部拒绝
    with pytest.raises(AuthError) as e1:
        validator.verify_device(OWNER_SECRET)
    assert e1.value.code == 40101
    with pytest.raises(AuthError) as e2:
        validator.verify_sidecar(SECRET_A)
    assert e2.value.code == 40101
    with pytest.raises(AuthError) as e3:
        validator.verify_owner(SECRET_A)
    assert e3.value.code == 40101
    with pytest.raises(AuthError) as e4:
        validator.verify_sidecar(OWNER_SECRET)
    assert e4.value.code == 40101
    with pytest.raises(AuthError) as e5:
        validator.verify_owner(SIDECAR_SECRET)
    assert e5.value.code == 40101
    with pytest.raises(AuthError) as e6:
        validator.verify_device(f"{DEVICE_A}.{SECRET_B}")
    assert e6.value.code == 40101
    # 正确主体必须通过
    assert validator.verify_device(f"{DEVICE_A}.{SECRET_A}").type == "device"
    assert validator.verify_sidecar(SIDECAR_SECRET).type == "sidecar"
    assert validator.verify_owner(OWNER_SECRET).type == "owner"


def test_missing_or_invalid_bearer_returns_40101(tmp_path: Path) -> None:
    store = _store(tmp_path)
    validator = _validator(store)
    for bad in ("", "Bearer", "not-a-token", "basic abc", "device-id-only."):
        with pytest.raises(AuthError) as e:
            validator.verify_device(bad)
        assert e.value.code == 40101
    with pytest.raises(AuthError) as e:
        validator.verify_sidecar("")
    assert e.value.code == 40101


def test_revoked_credential_returns_40103_and_never_leaks_token(tmp_path: Path, caplog) -> None:
    store = _store(tmp_path)
    store.revoke_device(DEVICE_A, reason="lost phone")
    validator = _validator(store)
    with pytest.raises(AuthError) as e:
        validator.verify_device(f"{DEVICE_A}.{SECRET_A}")
    assert e.value.code == 40103
    assert SECRET_A not in str(e.value)
    with caplog.at_level(logging.DEBUG):
        pass
    assert SECRET_A not in caplog.text


# ---------- 2. nonce：主体绑定 + 原子消费 ----------

def test_nonce_subject_bound_atomic_consume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = NonceService(store, ttl_seconds=300)
    nonce = uuid.uuid4().hex
    assert service.consume(CredentialPrincipal("device", DEVICE_A, "c1"), nonce) is True
    assert service.consume(CredentialPrincipal("device", DEVICE_A, "c1"), nonce) is False
    # 不同主体使用相同 nonce 字符串互不串扰
    assert service.consume(CredentialPrincipal("device", DEVICE_B, "c2"), nonce) is True
    # 过期 nonce 不可复用
    old_nonce = uuid.uuid4().hex
    assert service.consume(CredentialPrincipal("device", DEVICE_A, "c1"), old_nonce, now=time.time() - 1000) is True
    assert service.consume(CredentialPrincipal("device", DEVICE_A, "c1"), old_nonce) is False


def test_nonce_rejects_oversized_and_undersized(tmp_path: Path) -> None:
    """高压 H10：无上限时 200 字符 nonce 被接受入库；必须拒绝畸形放大。"""
    store = _store(tmp_path)
    service = NonceService(store, ttl_seconds=300)
    principal = CredentialPrincipal("device", DEVICE_A, "c1")
    # 过长拒绝（上限 128）
    assert service.consume(principal, "x" * 129) is False
    assert service.consume(principal, "y" * 200) is False
    # 恰在上限内的合法形态可用（4 * 32 hex = 128）
    assert service.consume(principal, uuid.uuid4().hex * 4) is True
    # 过短拒绝（<16）
    assert service.consume(principal, "short") is False


# ---------- 3. 限流：device/IP 双键 + 窗口恢复 ----------

def test_rate_limit_device_and_ip_keys_with_window_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    limiter = RateLimiter(
        store, RateLimitConfig(window_seconds=60, device_limit=3, ip_limit=5)
    )
    # device 限额：第 4 次被拒
    for i in range(3):
        allowed, retry = limiter.check(DEVICE_A, "1.1.1.1", "route:session", now=100.0)
        assert allowed is True
        assert retry is None
    allowed, retry = limiter.check(DEVICE_A, "1.1.1.1", "route:session", now=100.0)
    assert allowed is False
    assert retry is not None and retry > 0
    # 窗口恢复：越过窗口后允许
    allowed, _ = limiter.check(DEVICE_A, "1.1.1.1", "route:session", now=160.0)
    assert allowed is True


def test_rate_limit_ip_key_cannot_be_bypassed_by_new_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    limiter = RateLimiter(
        store, RateLimitConfig(window_seconds=60, device_limit=100, ip_limit=3)
    )
    for _ in range(3):
        allowed, _ = limiter.check(DEVICE_A, "2.2.2.2", "route:session", now=200.0)
        assert allowed is True
    # 换 token（设备 B）不绕过 IP 限流
    allowed, retry = limiter.check(DEVICE_B, "2.2.2.2", "route:session", now=200.0)
    assert allowed is False
    assert retry is not None


def test_rate_limit_device_key_cannot_be_bypassed_by_new_ip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    limiter = RateLimiter(
        store, RateLimitConfig(window_seconds=60, device_limit=2, ip_limit=100)
    )
    for _ in range(2):
        allowed, _ = limiter.check(DEVICE_A, "3.3.3.3", "route:session", now=300.0)
        assert allowed is True
    allowed, _ = limiter.check(DEVICE_A, "3.3.3.4", "route:session", now=300.0)
    assert allowed is False


def test_rate_limit_default_config_small_window(tmp_path: Path) -> None:
    """高压 H3/H9：默认 60s 大窗口对慢速滥用不敏感；生产默认已细化为 10s 窗口。
    每分钟语义等价（device 60/min、ip 120/min），但 10s 粒度捕捉突发。"""
    config = RateLimitConfig()
    assert config.window_seconds == 10
    assert config.device_limit == 10
    assert config.ip_limit == 20
    # 10s 窗口内第 11 次被拒
    store = _store(tmp_path)
    limiter = RateLimiter(store, config)
    for i in range(10):
        allowed, _ = limiter.check(DEVICE_A, "4.4.4.4", "route:session", now=400.0)
        assert allowed is True
    allowed, retry = limiter.check(DEVICE_A, "4.4.4.4", "route:session", now=400.0)
    assert allowed is False
    assert retry is not None
    # 越过窗口恢复
    allowed, _ = limiter.check(DEVICE_A, "4.4.4.4", "route:session", now=410.0)
    assert allowed is True


# ---------- 4. userSig TTL 与主体绑定（独立解析，不只看配置） ----------

def test_user_sig_ttl_bounded_and_subject_bound() -> None:
    sdk_app_id = 1600155678
    secret_key = "fake-secret-key-for-test-only-0123456789"
    sig = gen_user_sig(sdk_app_id, secret_key, DEVICE_A, expire_s=600)
    payload = parse_user_sig(sig)
    assert 0 < int(payload["TLS.expire"]) <= 600
    assert payload["TLS.identifier"] == DEVICE_A
    assert int(payload["TLS.sdkappid"]) == sdk_app_id
    assert user_sig_expire_ok(sig, max_expire_s=600) is True
    over = gen_user_sig(sdk_app_id, secret_key, DEVICE_A, expire_s=601)
    assert user_sig_expire_ok(over, max_expire_s=600) is False


# ---------- 5. 生产 fail-closed 矩阵 ----------

@pytest.mark.parametrize(
    "field",
    [
        "tls_enabled",
        "owner_credential_hash",
        "sidecar_credential_hash",
        "nonce_enabled",
        "rate_limit_enabled",
        "trtc_sdk_app_id",
        "trtc_secret_key",
    ],
)
def test_production_fail_closed_each_missing_item(field: str) -> None:
    security = _full_security()
    setattr(security, field, "" if isinstance(getattr(security, field), str) else False)
    missing = validate_production(security)
    assert field in missing
    with pytest.raises(ProductionGateError):
        from app.api.routes_voice import create_secured_voice_router
        from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
        from app.voice.storage import VoiceStore
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            store = VoiceStore(Path(td) / "v.db")
            store.initialize()
            create_secured_voice_router(
                store=store,
                service=RtcSessionService(
                    RtcSessionConfig(sdk_app_id=security.trtc_sdk_app_id or 1,
                                     secret_key=security.trtc_secret_key or "x",
                                     room_prefix="jax-")
                ),
                validator=CredentialValidator(store, security.owner_credential_hash,
                                              security.sidecar_credential_hash),
                nonces=NonceService(store),
                limiter=RateLimiter(store, RateLimitConfig()),
                security=security,
            )


def test_production_rejects_http_endpoint() -> None:
    security = _full_security()
    # 生产只允许 TLS；http/ws 明文端点视为缺失 TLS
    security.tls_enabled = False
    assert "tls_enabled" in validate_production(security)


# ---------- 6. 统一错误码 ----------

def test_error_payload_and_status_table() -> None:
    payload = error_payload(40102)
    assert payload == {"code": 40102, "data": None, "message": "nonce_replay"}
    assert HTTP_STATUS[40101] == 401
    assert HTTP_STATUS[40102] == 401
    assert HTTP_STATUS[40103] == 401
    assert HTTP_STATUS[42901] == 429
    assert HTTP_STATUS[50300] == 503
    # 2026-08-13 新增 50301 termination_unconfirmed（revoke 终止未确认）
    assert HTTP_STATUS[50301] == 503
    assert error_payload(50301) == {"code": 50301, "data": None, "message": "termination_unconfirmed"}
    # 2026-08-13 ADR-021 新增 40301 privacy_disabled / 50302 privacy_action_failed
    assert HTTP_STATUS[40301] == 403
    assert error_payload(40301) == {"code": 40301, "data": None, "message": "privacy_disabled"}
    assert HTTP_STATUS[50302] == 503
    assert error_payload(50302) == {"code": 50302, "data": None, "message": "privacy_action_failed"}
    assert len(ERROR_MESSAGES) == 14
    err = VoiceError(42901)
    assert err.code == 42901
    assert err.message == "rate_limited"
