"""外带上行 ingest-ticket 签发契约（阶段 1：控制面签发端）。

设计依据：outputs/design-uplink-out-of-band-2026-09-18.md §1/§2。
本文件只钉**签发端**契约，桥侧验签在后续阶段接入。

承重断言（这些是本阶段真正的交付物，不是陪衬）：
1. ticket 用 **hello 同一把公钥**可验，且 `aud="rtc_bridge_ingest"`；
2. **audience 混淆必须被拒**：hello proof 不得被当成 ingest ticket 接受（反向亦然）——
   这是把两类凭证分开的唯一机制，一旦失效，sidecar 的 hello 就能被复用为媒体入站凭证；
3. TTL 硬上界 300s，且**构造参数放宽也会被夹回**（不能靠调用方自觉）；
4. 只有绑定到**真实且可签发状态**的会话才签发；未知会话/已终止会话一律拒；
5. 端点 fail-closed 于设备 Bearer + nonce；且**未认证请求不得泄露 body schema**
   （必须 401 而非 422 —— 这是 guarded_route.py 存在的原因）；
6. 未装配签名器时端点 503，绝不静默降级。

自包含 fixture/helper（tests/contract 无 __init__.py，rootdir 导入模式）。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_voice_secured import create_secured_voice_router
from app.voice.auth import CredentialValidator
from app.voice.config import VoiceSecurityConfig, build_sidecar_credential_hashes
from app.voice.hello_proof import (
    HELLO_KID, ISSUER, HelloProofError, HelloProofSigner, verify_hello_proof,
)
# 模块级导入：下面 _ingest_claims 等模块级 helper 需要它。若该模块缺失，本文件在
# **收集期**就红（比 fixture 期更早），这比把 import 藏进函数体更符合 RED-first。
from app.voice.ingest_ticket import INGEST_AUDIENCE
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.rtc_session import RtcSessionConfig, RtcSessionService
from app.voice.storage import VoiceStore

INGEST_ENDPOINT = "/api/v1/voice/session/ingest-ticket"

DEVICE_A = "dev-a-000000000000000000000001"
DEVICE_B = "dev-b-000000000000000000000002"
SECRET_A = "secret-a-0123456789abcdef01234567"
SECRET_B = "secret-b-0123456789abcdef01234567"
OWNER_SECRET = "owner-secret-0123456789abcdef0123"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
FAKE_SDK_APP_ID = 1600155678
FAKE_SECRET_KEY = "fake-secret-key-for-test-only-0123456789"


def nonce() -> str:
    return uuid.uuid4().hex


def _pems() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


class IngestFixture:
    """真实 FastAPI/SQLite 装配（与 tests/integration/voice_security_fixture 同构，只取所需）。"""

    def __init__(self, tmp_path: Path, *, with_signer: bool = True) -> None:
        self.store = VoiceStore(tmp_path / "voice.db")
        self.store.initialize()
        self.store.save_device(DEVICE_A, SECRET_A, device_name="phone-a")
        self.store.save_device(DEVICE_B, SECRET_B, device_name="phone-b")

        sidecar_credentials = build_sidecar_credential_hashes(
            current_secret=SIDECAR_SECRET, next_secret="",
            next_enabled_at="", next_expires_at="", config_revision="",
        )
        security = VoiceSecurityConfig(
            production=False,
            tls_enabled=True,
            owner_credential_hash=CredentialValidator.hash_credential(OWNER_SECRET),
            sidecar_credential_hash=sidecar_credentials.current_hash,
            nonce_enabled=True,
            rate_limit_enabled=True,
            trtc_sdk_app_id=FAKE_SDK_APP_ID,
            trtc_secret_key=FAKE_SECRET_KEY,
        )
        self.security = security
        self.validator = CredentialValidator(
            self.store,
            security.owner_credential_hash,
            sidecar_credentials,
            clock=lambda: datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc).timestamp(),
        )
        self.nonces = NonceService(self.store, ttl_seconds=300)
        self.limiter = RateLimiter(
            self.store,
            RateLimitConfig(window_seconds=60, device_limit=1000, ip_limit=1000),
        )
        self.service = RtcSessionService(
            RtcSessionConfig(
                sdk_app_id=FAKE_SDK_APP_ID, secret_key=FAKE_SECRET_KEY, room_prefix="jax-",
            )
        )
        self.private_pem, self.public_pem = _pems()
        self.hello_signer = HelloProofSigner(self.private_pem)

        from app.voice.ingest_ticket import IngestTicketSigner

        self.ingest_signer = IngestTicketSigner(self.private_pem) if with_signer else None

        self.app = FastAPI()
        self.app.include_router(
            create_secured_voice_router(
                store=self.store, service=self.service, validator=self.validator,
                nonces=self.nonces, limiter=self.limiter, security=security,
                ingest_ticket_signer=self.ingest_signer,
            )
        )
        self.client = TestClient(self.app)

    # ---- helpers ----
    def auth_headers(self, device_id: str = DEVICE_A, secret: str = SECRET_A) -> dict:
        return {
            "Authorization": f"Bearer {device_id}.{secret}",
            "X-Request-Nonce": nonce(),
        }

    def create_session(self, device_id: str = DEVICE_A, secret: str = SECRET_A) -> dict:
        resp = self.client.post(
            "/api/v1/voice/session",
            json={"device_id": device_id, "entry_point": "main"},
            headers=self.auth_headers(device_id, secret),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    def request_ticket(self, session_id: str, device_id: str = DEVICE_A,
                       secret: str = SECRET_A) -> "object":
        return self.client.post(
            INGEST_ENDPOINT,
            json={"device_id": device_id, "session_id": session_id},
            headers=self.auth_headers(device_id, secret),
        )

    def force_session_state(self, session_id: str, state: str) -> None:
        """直接改账本状态：契约测试要能构造「不可签发」的会话终态。"""
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE control_plane_sessions SET state = ? WHERE session_id = ?",
                (state, session_id),
            )
            conn.commit()


@pytest.fixture()
def fx(tmp_path: Path) -> IngestFixture:
    return IngestFixture(tmp_path)


# ---------------------------------------------------------------- 1. 正常签发

def test_issued_ticket_verifies_with_hello_public_key(fx: IngestFixture) -> None:
    """ticket 必须用**已有的** hello 公钥可验 —— 不引入第二把密钥。"""
    from app.voice.ingest_ticket import INGEST_AUDIENCE, verify_ingest_ticket

    session = fx.create_session()
    resp = fx.request_ticket(session["session_id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]

    claims = verify_ingest_ticket(data["ticket"], fx.public_pem)

    assert claims["aud"] == INGEST_AUDIENCE == "rtc_bridge_ingest"
    assert claims["sid"] == session["session_id"]
    assert claims["did"] == DEVICE_A
    assert claims["rid"] == session["room_id"]
    assert claims["gen"] == session["generation"]
    assert isinstance(claims["jti"], str) and claims["jti"]
    assert data["session_id"] == session["session_id"]
    assert 0 < data["ttl_seconds"] <= 300


def test_issued_ticket_binds_to_the_session_it_was_issued_for(fx: IngestFixture) -> None:
    """验签方可按会话绑定校验；绑错会话必须被拒。"""
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    session = fx.create_session()
    ticket = fx.request_ticket(session["session_id"]).json()["data"]["ticket"]

    with pytest.raises(IngestTicketError):
        verify_ingest_ticket(
            ticket, fx.public_pem, expected_session_id="some-other-session",
        )


# ------------------------------------------- 2. audience 混淆（承重负向测试）

def test_hello_proof_is_not_accepted_as_ingest_ticket(fx: IngestFixture) -> None:
    """承重：hello proof 的 aud 是 rtc_bridge，绝不能被当成 ingest ticket。

    若这条失效，sidecar 的 hello 就能直接换成媒体入站凭证 —— audience 是唯一的分离机制。
    """
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    hello = fx.hello_signer.issue({
        "session_id": str(uuid.uuid4()), "device_id": DEVICE_A,
        "room_id": f"jax-{DEVICE_A}", "sidecar_user_id": "jax-pc-sidecar",
        "generation": 0,
    })
    assert hello["proof"]

    with pytest.raises(IngestTicketError):
        verify_ingest_ticket(hello["proof"], fx.public_pem)


def test_ingest_ticket_is_not_accepted_as_hello_proof(fx: IngestFixture) -> None:
    """反向不变式：hello_proof 的既有验证语义不得被本特性放宽。"""
    session = fx.create_session()
    ticket = fx.request_ticket(session["session_id"]).json()["data"]["ticket"]

    with pytest.raises(HelloProofError):
        verify_hello_proof(ticket, fx.public_pem)


def _sign_claims(fx: IngestFixture, claims: dict, *,
                 key_pem: str | None = None,
                 header: dict | None = None) -> str:
    """用真实（或指定的）私钥签任意声明 dict，供逐判据隔离测试使用。"""
    import jwt as pyjwt

    return pyjwt.encode(
        claims, key_pem or fx.private_pem, algorithm="EdDSA",
        headers=header or {"alg": "EdDSA", "typ": "JWT", "kid": HELLO_KID},
    )


def _ingest_claims(*, audience: object = None, ttl_seconds: int = 300,
                   now: int | None = None, **overrides: object) -> dict:
    """一枚「形状全对」的 ingest 声明集；各项可单独覆盖/删除。

    默认值与 _REQUIRED_CLAIMS 对齐，因此除被测的那一条外没有别的拒签理由。
    """
    now = int(time.time()) if now is None else now
    claims: dict = {
        "iss": ISSUER,
        "aud": INGEST_AUDIENCE if audience is None else audience,
        "jti": uuid.uuid4().hex,
        "sid": "s-forged", "did": DEVICE_A, "rid": f"jax-{DEVICE_A}", "gen": 0,
        "iat": now, "exp": now + ttl_seconds,
    }
    for key, value in overrides.items():
        if value is _DROP:
            claims.pop(key, None)
        else:
            claims[key] = value
    return claims


class _Drop:
    """覆盖时用它表示「删掉这个声明」。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return "<DROP>"


_DROP = _Drop()


def _forge(fx: IngestFixture, *, audience: object, ttl_seconds: int, now: int) -> str:
    """用**真实私钥**手工签一枚 token，用于把单一判据隔离出来测。

    声明形状与 ingest ticket 完全一致（sid/did/rid/gen/jti 齐全、TTL 可控），
    因此除了被测的那一条（aud 或 TTL），没有任何其他理由该被拒。
    """
    return _sign_claims(fx, _ingest_claims(audience=audience, ttl_seconds=ttl_seconds, now=now))



def test_ingest_shaped_token_with_hello_audience_is_rejected(fx: IngestFixture) -> None:
    """隔离 audience 判据：声明形状全对、密钥对，**只有 aud 是 hello 的** ⇒ 必须拒。

    这条是「aud 是两类凭证的唯一分离机制」的直接检验：一旦验签侧不再校验 audience，
    它会立刻变红（见本轮变异验证）。
    """
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _forge(fx, audience="rtc_bridge", ttl_seconds=300, now=now)

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


def test_audience_as_list_containing_the_right_value_is_rejected(fx: IngestFixture) -> None:
    """**精确相等，不是集合成员**：aud 写成 ["rtc_bridge_ingest"] 也必须拒。

    这是 M1 教训的正面钉子。JWT 的 aud 允许是数组，而 PyJWT 的 `audience=` 语义正是
    「集合内任一匹配」——若沿用库语义，这条会**被接受**。我们把库校验关掉改成 `!=`，
    换来的就是它必须被拒。这条一旦变红，说明有人把精确比对退回了成员判定。
    """
    from app.voice.ingest_ticket import INGEST_AUDIENCE, IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _sign_claims(fx, _ingest_claims(audience=[INGEST_AUDIENCE], now=now))

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


def test_verifier_rejects_ttl_beyond_300s(fx: IngestFixture) -> None:
    """隔离 TTL 上界：**签发侧夹紧**之外，验签侧必须独立复核。

    否则一条手滑签出的长时效 token 仍会被接受（签发侧夹紧只挡自家调用方，
    挡不住任何其他持有私钥/历史版本的签发路径）。
    """
    from app.voice.ingest_ticket import INGEST_AUDIENCE, IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _forge(fx, audience=INGEST_AUDIENCE, ttl_seconds=3600, now=now)

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40112


# ------------------------------------------------------------- 3. TTL 上界

def test_ticket_ttl_never_exceeds_300s(fx: IngestFixture) -> None:
    from app.voice.ingest_ticket import verify_ingest_ticket

    session = fx.create_session()
    ticket = fx.request_ticket(session["session_id"]).json()["data"]["ticket"]
    claims = verify_ingest_ticket(ticket, fx.public_pem)
    assert claims["exp"] - claims["iat"] <= 300


def test_signer_clamps_oversized_ttl(fx: IngestFixture) -> None:
    """调用方传 9999 也必须被夹回 300 —— 上界不能靠调用方自觉。"""
    from app.voice.ingest_ticket import IngestTicketSigner, verify_ingest_ticket

    signer = IngestTicketSigner(fx.private_pem, ttl_seconds=9999)
    issued = signer.issue(
        session_id="s-1", device_id=DEVICE_A, room_id=f"jax-{DEVICE_A}", generation=0,
    )
    claims = verify_ingest_ticket(issued["ticket"], fx.public_pem)
    assert claims["exp"] - claims["iat"] <= 300
    assert issued["ttl_seconds"] <= 300


def test_expired_ticket_is_rejected(fx: IngestFixture) -> None:
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    session = fx.create_session()
    ticket = fx.request_ticket(session["session_id"]).json()["data"]["ticket"]
    claims = verify_ingest_ticket(ticket, fx.public_pem)

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(ticket, fx.public_pem, now=claims["exp"] + 1)
    assert exc.value.code == 40112


# ------------------------------------------------- 4. 会话存在性 / 可签发状态

def test_unknown_session_is_rejected(fx: IngestFixture) -> None:
    resp = fx.request_ticket("00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == 40402


@pytest.mark.parametrize("state", ["TERMINATING", "TERMINATED", "KWS_READY"])
def test_non_issuable_session_state_is_rejected(fx: IngestFixture, state: str) -> None:
    session = fx.create_session()
    fx.force_session_state(session["session_id"], state)

    resp = fx.request_ticket(session["session_id"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == 40901


def test_other_devices_session_is_rejected(fx: IngestFixture) -> None:
    """A 的会话不得被 B 的凭证签票（跨设备越权）。"""
    session = fx.create_session(DEVICE_A, SECRET_A)

    resp = fx.request_ticket(session["session_id"], DEVICE_B, SECRET_B)
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 40001


def test_device_id_must_match_bearer_subject(fx: IngestFixture) -> None:
    session = fx.create_session(DEVICE_A, SECRET_A)

    resp = fx.request_ticket(session["session_id"], DEVICE_B, SECRET_B)
    assert resp.json()["code"] == 40001


# ------------------------------------------------------- 5. 认证前置 / 不泄露 schema

def test_endpoint_requires_bearer(fx: IngestFixture) -> None:
    """端点内兜底：无 Bearer 时 subject 缺失 ⇒ 401/40101。

    注意这条**不覆盖**「未认证不得泄露 body schema」——变异 M3（守卫不再拒绝）下它
    仍然全绿，因为端点自身的 subject_id 兜底也返回 40101。schema 泄露只由
    test_unauthenticated_request_does_not_leak_body_schema 钉住（它才是 GuardedAPIRoute
    这一层的唯一守卫）。别把这条当成 401 优先的证明。
    """
    session = fx.create_session()
    resp = fx.client.post(
        INGEST_ENDPOINT, json={"device_id": DEVICE_A, "session_id": session["session_id"]},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_endpoint_requires_fresh_nonce(fx: IngestFixture) -> None:
    session = fx.create_session()
    headers = {"Authorization": f"Bearer {DEVICE_A}.{SECRET_A}", "X-Request-Nonce": nonce()}

    first = fx.client.post(
        INGEST_ENDPOINT,
        json={"device_id": DEVICE_A, "session_id": session["session_id"]},
        headers=headers,
    )
    assert first.status_code == 201, first.text

    replay = fx.client.post(
        INGEST_ENDPOINT,
        json={"device_id": DEVICE_A, "session_id": session["session_id"]},
        headers=headers,
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == 40102


def test_unauthenticated_request_does_not_leak_body_schema(fx: IngestFixture) -> None:
    """未认证 + 非法 body 必须 401（而非 422）—— 否则泄露 request schema。"""
    resp = fx.client.post(INGEST_ENDPOINT, json={"nope": 1})
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == 40101


def test_authenticated_invalid_body_is_rejected_422(fx: IngestFixture) -> None:
    """对照：认证通过后 body 校验照常生效（证明上一条不是因为 body 校验被关掉）。"""
    resp = fx.client.post(INGEST_ENDPOINT, json={"nope": 1}, headers=fx.auth_headers())
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------- 6. 未装配时 fail-closed

def test_endpoint_fails_closed_when_signer_not_wired(tmp_path: Path) -> None:
    unfitted = IngestFixture(tmp_path, with_signer=False)
    session = unfitted.create_session()

    resp = unfitted.request_ticket(session["session_id"])
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == 50300


def test_router_still_assembles_without_ingest_signer(tmp_path: Path) -> None:
    """可选装配不得破坏既有路由（回归护栏）。"""
    unfitted = IngestFixture(tmp_path, with_signer=False)
    assert unfitted.client.get("/api/v1/voice/status", headers=unfitted.auth_headers()).status_code in (200, 503)


# --------------------------------------- 7. 验签侧 fail-closed 分支覆盖（阶段 2）
#
# 这一节是**桥侧验签**（verify_ingest_ticket，纯函数、无 store）的契约。它是惰性的：
# 没有任何实时路径调用它，下面的 test_verifier_is_not_wired_into_any_live_path
# 把「惰性」从承诺变成被测试钉住的不变式。

def test_ticket_matching_expected_session_id_is_accepted(fx: IngestFixture) -> None:
    """正向对照：会话绑定**恰好匹配**时必须放行。

    没有这条，一个「把什么都拒掉」的过度绑定实现也能让下面所有负向测试变绿——
    这正是本仓库反复踩的「假绿」形态，负向断言必须有正向对照。
    """
    from app.voice.ingest_ticket import verify_ingest_ticket

    session = fx.create_session()
    ticket = fx.request_ticket(session["session_id"]).json()["data"]["ticket"]

    claims = verify_ingest_ticket(
        ticket, fx.public_pem, expected_session_id=session["session_id"],
    )
    assert claims["sid"] == session["session_id"]


def test_ticket_without_aud_is_rejected(fx: IngestFixture) -> None:
    """承重：aud 缺失必须拒。缺 aud 的票在语义上根本不是本类凭证。"""
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _sign_claims(fx, _ingest_claims(aud=_DROP, now=now))

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


def test_ticket_signed_by_a_different_key_is_rejected(fx: IngestFixture) -> None:
    """承重：**换一把私钥**签的票必须拒（签名校验真的在跑，不是只看形状）。

    形状与真票逐字段相同，唯一差别是签名者。若这条绿着，说明验签只看声明不看签名。
    """
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    other_private_pem, _ = _pems()
    now = int(time.time())
    forged = _sign_claims(fx, _ingest_claims(now=now), key_pem=other_private_pem)

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


def test_ticket_signed_by_the_hello_key_still_verifies(fx: IngestFixture) -> None:
    """对照：同一把 hello 私钥签的**同形**声明可以验过。

    与上一条成对：证明上一条红是因为密钥不对，不是因为 helper 本身签坏了。
    """
    from app.voice.ingest_ticket import verify_ingest_ticket

    now = int(time.time())
    good = _sign_claims(fx, _ingest_claims(now=now))

    assert verify_ingest_ticket(good, fx.public_pem, now=now)["aud"] == "rtc_bridge_ingest"


def test_tampered_ticket_payload_is_rejected(fx: IngestFixture) -> None:
    """改写 payload（如把 sid 换掉）但保留原签名 ⇒ 必须拒。

    这是「拿到票之后改内容」的最直接攻击面，也顺带证明 sid 绑定不能靠改 JSON 绕开。
    """
    import base64
    import json

    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    session = fx.create_session()
    ticket = fx.request_ticket(session["session_id"]).json()["data"]["ticket"]
    header_b64, payload_b64, signature_b64 = ticket.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    )
    payload["sid"] = "s-tampered"
    forged_payload = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    tampered = f"{header_b64}.{forged_payload}.{signature_b64}"

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(tampered, fx.public_pem, expected_session_id="s-tampered")
    assert exc.value.code == 40111


@pytest.mark.parametrize("dead", ["", "not-a-jwt", "a.b.c", "....", "eyJhbGciOiJIUzI1NiJ9"])
def test_malformed_ticket_is_rejected(fx: IngestFixture, dead: str) -> None:
    """畸形输入一律 40111，且**不抛裸异常**（验签方是 fail-closed 边界）。"""
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(dead, fx.public_pem)
    assert exc.value.code == 40111


def test_ticket_with_foreign_header_is_rejected(fx: IngestFixture) -> None:
    """header 必须**逐字段**等于本族 header（alg/typ/kid）。

    换个 kid 就说明这不是我们这一代的票——轮换期尤其要挡住「上一代密钥签的票」混进来。
    """
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _sign_claims(
        fx, _ingest_claims(now=now),
        header={"alg": "EdDSA", "typ": "JWT", "kid": "cp-hello-ed25519-v2"},
    )

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


@pytest.mark.parametrize("key", ["sid", "did", "rid", "jti"])
def test_missing_string_claim_is_rejected(fx: IngestFixture, key: str) -> None:
    """承重：sid/did/rid/jti 缺失即拒（会话绑定与可追溯性都依赖它们）。"""
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _sign_claims(fx, _ingest_claims(now=now, **{key: _DROP}))

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


@pytest.mark.parametrize("value", [12345, None, ["sid"], {"a": 1}])
def test_non_string_sid_is_rejected(fx: IngestFixture, value: object) -> None:
    """sid 必须是**非空字符串**：类型混淆会让 `claims["sid"] != expected` 静默成立或报错。"""
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _sign_claims(fx, _ingest_claims(now=now, sid=value))

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40111


def test_ticket_issued_in_the_future_is_rejected(fx: IngestFixture) -> None:
    """iat 晚于当前时刻 ⇒ 拒（控制面时钟错乱或伪造的时序凭证）。"""
    from app.voice.ingest_ticket import IngestTicketError, verify_ingest_ticket

    now = int(time.time())
    forged = _sign_claims(fx, _ingest_claims(now=now, iat=now + 60, exp=now + 300))

    with pytest.raises(IngestTicketError) as exc:
        verify_ingest_ticket(forged, fx.public_pem, now=now)
    assert exc.value.code == 40112


def test_verifier_is_not_wired_into_any_live_path() -> None:
    """把「惰性」变成被测试钉住的结构不变式，而不是一句承诺。

    阶段 2 只交付**纯函数 + 契约测试**。一旦有人在实时路径里调用它（监听器、桥、
    控制面），这条立刻变红——「不接线」就不能被后人无意破坏。
    sidecar/ 不在扫描范围：Node 无法 import Python 符号。
    """
    repo = Path(__file__).resolve().parents[3]
    defining = repo / "backend" / "app" / "voice" / "ingest_ticket.py"
    live_roots = ("backend/app", "backend/rtc_bridge", "cloudapi", "cloudbridge", "scripts")
    offenders: list[str] = []
    for rel in live_roots:
        base = repo / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if path == defining or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover - 权限边缘
                continue
            if "verify_ingest_ticket" in text:
                offenders.append(str(path.relative_to(repo)))
    assert not offenders, f"验签函数被接线到实时路径: {offenders}"
