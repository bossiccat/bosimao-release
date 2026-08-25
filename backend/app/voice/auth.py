"""凭证主体校验（SPEC §9.1 / ADR-014）

四类主体：owner / device / sidecar / session。主体来自服务端验证结果，
客户端不可自报。凭证只存哈希；敏感值不进入异常文本。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import (
    SidecarCredentialConfigError,
    SidecarCredentialHashSet,
    utc_now,
)
from .errors import ERROR_MESSAGES
from .storage import VoiceStore

PRINCIPAL_OWNER = "owner"
PRINCIPAL_DEVICE = "device"
PRINCIPAL_SIDECAR = "sidecar"
PRINCIPAL_SESSION = "session"
PRINCIPAL_RTC_BRIDGE = "rtc_bridge"
PRINCIPAL_BRAIN = "brain"


@dataclass(frozen=True)
class CredentialPrincipal:
    """服务端验证后的主体（type/subject_id/credential_id 均不可客户端自报）"""

    type: str
    subject_id: str
    credential_id: str


class AuthError(Exception):
    """认证失败：code 只取锁定错误码（40101/40103），message 不含敏感值"""

    def __init__(self, code: int, message: str | None = None) -> None:
        super().__init__(message or ERROR_MESSAGES[code])
        self.code = code
        self.message = message or ERROR_MESSAGES[code]


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class CredentialValidator:
    """owner/device/sidecar 三类静态或数据库凭证验证器"""

    def __init__(
        self,
        store: VoiceStore,
        owner_credential_hash: str = "",
        sidecar_credentials: SidecarCredentialHashSet | str | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        sidecar_credential_hash: str = "",
        rtc_bridge_credential_hash: str = "",
        brain_service_credential_hash: str = "",
    ) -> None:
        self._store = store
        self._owner_hash = owner_credential_hash
        # 方案 B ack 上报服务凭证（contract brainService / rtcBridgeService）；
        # 未配置（空哈希）时对应 verify_* 一律 40101 fail-closed。
        self._rtc_bridge_hash = rtc_bridge_credential_hash
        self._brain_hash = brain_service_credential_hash
        legacy_hash = sidecar_credential_hash
        if isinstance(sidecar_credentials, str) and not legacy_hash:
            legacy_hash = sidecar_credentials
            sidecar_credentials = None
        if sidecar_credentials is not None and legacy_hash:
            self._sidecar_credentials = SidecarCredentialHashSet("invalid")
        elif isinstance(sidecar_credentials, str):
            self._sidecar_credentials = SidecarCredentialHashSet(sidecar_credentials)
        elif sidecar_credentials is not None:
            self._sidecar_credentials = sidecar_credentials
        elif legacy_hash:
            self._sidecar_credentials = SidecarCredentialHashSet(legacy_hash)
        else:
            self._sidecar_credentials = None
        self._clock = clock

    @staticmethod
    def hash_credential(secret: str) -> str:
        """静态凭证（owner/sidecar）哈希：SHA-256 + 固定盐前缀"""
        salt = "jax-static-v1"
        return f"{salt}${hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()}"

    def _verify_static(self, secret: str, stored_hash: str) -> bool:
        if not stored_hash:
            return False
        salt, digest = stored_hash.split("$", 1)
        candidate = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
        return _constant_time_equal(candidate, digest)

    def verify_owner(self, bearer: str) -> CredentialPrincipal:
        if not bearer or not self._verify_static(bearer, self._owner_hash):
            raise AuthError(40101)
        fingerprint = hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:16]
        return CredentialPrincipal(PRINCIPAL_OWNER, "owner", f"owner-{fingerprint}")

    def verify_sidecar(self, bearer: str) -> CredentialPrincipal:
        if not bearer:
            raise AuthError(40101)
        credentials = self._sidecar_credentials
        try:
            if credentials is None:
                raise SidecarCredentialConfigError("sidecar credentials unavailable")
            state = credentials.rotation_state(self._clock())
            salt, current_digest = credentials.current_hash.split("$", 1)
            candidate = hashlib.sha256((salt + bearer).encode("utf-8")).hexdigest()
            current_match = hmac.compare_digest(candidate, current_digest)
            if state == "rotation_active":
                assert credentials.next_hash is not None
                _, next_digest = credentials.next_hash.split("$", 1)
                next_match = hmac.compare_digest(candidate, next_digest)
                accepted = current_match | next_match
            else:
                accepted = current_match
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(50300) from exc
        if not accepted:
            raise AuthError(40101)
        return CredentialPrincipal(PRINCIPAL_SIDECAR, "sidecar", "sidecar-credential")

    def verify_device(self, bearer: str) -> CredentialPrincipal:
        """Bearer 格式：<device_id>.<credential_secret>；按 token 内 device_id 校验哈希与撤销。

        body 与主体的一致性由路由层比对 principal.subject_id 处理（40001）。
        """
        if not bearer or "." not in bearer:
            raise AuthError(40101)
        token_device_id, secret = bearer.split(".", 1)
        if not token_device_id or not secret:
            raise AuthError(40101)
        row = self._store.verify_device_secret(token_device_id, secret)
        if row is None:
            raise AuthError(40101)
        if row.status == "revoked" or row.revoked_at is not None:
            raise AuthError(40103)
        if row.expires_at < time.time():
            raise AuthError(40103)
        return CredentialPrincipal(PRINCIPAL_DEVICE, token_device_id, row.credential_id)

    def _verify_service_hash(self, bearer: str, stored_hash: str,
                             principal_type: str, subject_id: str,
                             credential_id: str) -> CredentialPrincipal:
        """静态服务凭证验证（rtc_bridge / brain）；未配置即拒绝。"""
        if not bearer or not self._verify_static(bearer, stored_hash):
            raise AuthError(40101)
        return CredentialPrincipal(principal_type, subject_id, credential_id)

    def verify_rtc_bridge(self, bearer: str) -> CredentialPrincipal:
        """rtc_bridge 服务身份（ack 上报 A3/A4 与 hello proof 签发对象）。"""
        return self._verify_service_hash(
            bearer, self._rtc_bridge_hash, PRINCIPAL_RTC_BRIDGE,
            "rtc_bridge", "rtc-bridge-credential",
        )

    def verify_brain(self, bearer: str) -> CredentialPrincipal:
        """Brain ingress/outbox 服务身份（ack 上报 brain_turns_sealed）。"""
        return self._verify_service_hash(
            bearer, self._brain_hash, PRINCIPAL_BRAIN,
            "brain", "brain-service-credential",
        )
