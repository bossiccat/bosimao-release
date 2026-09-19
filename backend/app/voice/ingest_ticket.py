"""会话级、短时效的外带上行 ingest ticket（设计：outputs/design-uplink-out-of-band-2026-09-18.md §1/§2）。

为什么复用 hello 的 Ed25519 密钥对，而不是新增一把 HMAC 共享密钥
--------------------------------------------------------------
桥容器**没有设备凭证库**（它只认控制面 mTLS 兑付），所以验签必须**无 store**；
而 `hello_proof.verify_hello_proof` 恰好就是这样一个纯函数。复用同一密钥对带来三件事：
1. 验签原语已经存在并被测试覆盖，改动面最小；
2. 桥容器只需持有**公钥**，"不持长期签名秘密"的既有安全不变式原样保持；
3. 轮换只分发公钥（非秘密材料），且天然可双公钥过渡；HMAC 方案每次轮换都要在两个
   CloudRun 服务间同步秘密——那正是 config.py:80-89 记过的漂移类事故形态。

与 hello proof 的唯一分离机制是 audience
----------------------------------------
    hello proof  : aud = "rtc_bridge"          （既定，不改）
    ingest ticket: aud = "rtc_bridge_ingest"   （本模块）

两者**必须互不接受**。本模块解码后**显式比对** `claims["aud"] == INGEST_AUDIENCE`
（不依赖 PyJWT 的默认 aud 行为，理由见 `verify_ingest_ticket` 内注释），因此
hello proof 落到这里必然被拒；反向由 `verify_hello_proof` 自己的 aud 校验保证。这是把
「边车进房凭证」与「媒体入站凭证」分开的唯一机制——一旦失效，一个 hello proof 就能被
复用为媒体入站凭证。`hello_proof.verify_hello_proof` 的既有语义**不得**因本特性改动
（契约测试有反向断言）。

TTL 上界是硬约束
----------------
`IngestTicketSigner` 在**构造时夹紧** ttl 到 [1, 300]：上界不能依赖调用方自觉，
否则一个手滑的 `ttl_seconds=86400` 就把「短时效」变成全天有效。
验签侧同样复核 `exp - iat <= 300`（签得再宽也验不过）。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import jwt

from .hello_proof import HELLO_KID, ISSUER

#: 本凭证的 audience。与 hello proof 的 "rtc_bridge" 严格区分。
INGEST_AUDIENCE = "rtc_bridge_ingest"

#: TTL 硬上界（秒）。签发与验签两侧都必须遵守。
INGEST_MAX_TTL_SECONDS = 300

#: 默认 TTL（秒）。
INGEST_DEFAULT_TTL_SECONDS = 300

#: 允许签发 ingest ticket 的会话状态。
#:
#: 显式白名单（而不是「排除终态」）：未知状态一律拒绝，fail-closed。
#: - SIGNING / ENTERING：会话已签发、边车正在兑付进房——手机此时已在取票，属正常时序；
#: - ACTIVE：媒体面已建立，主路径。
#: TERMINATING / TERMINATED / TERMINATION_* / KWS_READY 一律不可签发。
INGEST_ALLOWED_SESSION_STATES = frozenset({"SIGNING", "ENTERING", "ACTIVE"})

#: 与 hello proof 同族 header：同一把密钥、同一个 kid。区分靠 aud，不靠 kid。
_HEADER = {"alg": "EdDSA", "typ": "JWT", "kid": HELLO_KID}

#: 必需声明（缺失即拒）。短名 sid/did/rid/gen 与设计文档 §1 一致。
_REQUIRED_CLAIMS = (
    "iss", "aud", "jti", "sid", "did", "rid", "gen", "iat", "exp",
)

_STRING_CLAIMS = ("sid", "did", "rid", "jti")


class IngestTicketError(Exception):
    """ticket 被拒。40111=无效，40112=过期（与 hello family 同码，便于统一错误词汇）。"""

    def __init__(self, code: int) -> None:
        super().__init__("ingest ticket rejected")
        self.code = code


def _clamp_ttl(ttl_seconds: int) -> int:
    try:
        value = int(ttl_seconds)
    except (TypeError, ValueError):
        return INGEST_DEFAULT_TTL_SECONDS
    return max(1, min(value, INGEST_MAX_TTL_SECONDS))


def _iso8601(epoch_seconds: int) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class IngestTicketSigner:
    """用 hello 私钥签发会话级 ingest ticket。

    注意：与 `HelloProofSigner` 一致，构造期**不解析** PEM——只校验非空。
    这一点是刻意的：生产装配在启动期不应因密钥格式问题崩溃（fail-closed 由签名时的
    异常 + 端点的 503 分支承担），且既有 cloudapi 装配契约测试使用占位 PEM。
    """

    def __init__(
        self,
        private_key_pem: str,
        *,
        ttl_seconds: int = INGEST_DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not private_key_pem:
            raise ValueError("ingest ticket signing key unavailable")
        self._private_key = private_key_pem
        self._ttl_seconds = _clamp_ttl(ttl_seconds)
        self._clock = clock

    def issue(
        self,
        *,
        session_id: str,
        device_id: str,
        room_id: str,
        generation: int,
    ) -> dict[str, object]:
        """签发一张绑定会话的 ticket。返回 ticket 与其明文有效期。"""
        context = {
            "sid": session_id, "did": device_id, "rid": room_id, "gen": generation,
        }
        if any(not isinstance(context[key], str) or not context[key]
               for key in ("sid", "did", "rid")):
            raise ValueError("ingest ticket context incomplete")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("ingest ticket generation invalid")

        issued_at = int(self._clock())
        claims = {
            "iss": ISSUER,
            "aud": INGEST_AUDIENCE,
            "jti": str(uuid.uuid4()),
            **context,
            "iat": issued_at,
            "exp": issued_at + self._ttl_seconds,
        }
        ticket = jwt.encode(
            claims, self._private_key, algorithm="EdDSA", headers=dict(_HEADER)
        )
        return {
            "ticket": ticket,
            "session_id": session_id,
            "ttl_seconds": self._ttl_seconds,
            "expires_at": _iso8601(claims["exp"]),
        }


def verify_ingest_ticket(
    ticket: str,
    public_key_pem: str,
    *,
    now: float | None = None,
    expected_session_id: str | None = None,
) -> dict:
    """无状态验签（桥容器用；不需要任何存储）。

    `now` 可注入，便于测试过期分支而不动系统时钟。
    `expected_session_id` 非空时强制会话绑定（桥侧只应接受当前活动会话的 ticket）。
    """
    if not ticket or not public_key_pem:
        raise IngestTicketError(40111)
    try:
        if jwt.get_unverified_header(ticket) != _HEADER:
            raise IngestTicketError(40111)
        claims = jwt.decode(
            ticket,
            public_key_pem,
            algorithms=["EdDSA"],
            issuer=ISSUER,
            options={
                "verify_exp": False, "verify_iat": False,
                # 刻意关掉 PyJWT 自带的 aud 校验，改由下面**本模块显式比对**：
                #   (a) 显式判据才能被变异测试证明「aud 是唯一分离机制」是承重的
                #       ——依赖库的默认行为会让这条测试因为偶然而通过（见变异记录 M1）；
                #   (b) 语义更强：库的语义是「集合内任一匹配」，我们要的是精确相等。
                # aud 的存在性仍由 _REQUIRED_CLAIMS 里的 "aud" 强制。
                "verify_aud": False,
                "require": list(_REQUIRED_CLAIMS),
            },
        )
    except IngestTicketError:
        raise
    except jwt.PyJWTError as exc:
        raise IngestTicketError(40111) from exc

    # audience 是本凭证与 hello proof 的**唯一**分离机制：只认 rtc_bridge_ingest。
    # 反向由 hello_proof.verify_hello_proof 自己的 aud 校验保证（契约测试有反向断言）。
    if claims.get("aud") != INGEST_AUDIENCE:
        raise IngestTicketError(40111)

    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    current = int(time.time() if now is None else now)
    if (
        not isinstance(issued_at, int) or isinstance(issued_at, bool)
        or not isinstance(expires_at, int) or isinstance(expires_at, bool)
        or expires_at <= issued_at
        # TTL 上界在验签侧复核：签得再宽也过不去（签发侧另有夹紧）
        or expires_at - issued_at > INGEST_MAX_TTL_SECONDS
        or issued_at > current
        or expires_at <= current
    ):
        raise IngestTicketError(40112)

    generation = claims.get("gen")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise IngestTicketError(40111)
    if any(not isinstance(claims.get(key), str) or not claims[key] for key in _STRING_CLAIMS):
        raise IngestTicketError(40111)
    if expected_session_id is not None and claims["sid"] != expected_session_id:
        raise IngestTicketError(40111)
    return claims
