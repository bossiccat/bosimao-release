"""设备生命周期与强一致撤销服务（SPEC §5 / AC-02 / AC-15）

- pairing_code：owner 生成，TTL<=300，只存 code_hash，原子单次消费
- register：pairing_code 为 bootstrap 主体，原子注册并一次性返回 credential_secret
- revoke：单事务内 credential 立即失效 + 未过期 userSig 指纹登记 + 终止事件 + 审计；
  外部进程终止失败返回明确错误（50301 termination_unconfirmed），不报告虚假成功
  （2026-08-13 自 50401 迁移：504 是网关超时语义，503 才是可重试的服务暂不可用）
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass

from .errors import VoiceError
from .storage import VoiceStore
from .termination import RtcSessionTerminator, UnavailableRtcSessionTerminator

PAIRING_TTL_SECONDS = 300
CREDENTIAL_TTL_DAYS = 30


@dataclass(frozen=True)
class DeviceRegistration:
    device_id: str
    credential_id: str
    credential_secret: str
    expires_at: float


class DeviceNotFoundError(VoiceError):
    def __init__(self) -> None:
        super().__init__(40401)


class PairingInvalidError(VoiceError):
    def __init__(self) -> None:
        super().__init__(40901)


class RevokeTerminationError(VoiceError):
    def __init__(self) -> None:
        super().__init__(50301)


class DeviceService:
    def __init__(self, store: VoiceStore,
                 terminator: RtcSessionTerminator | None = None) -> None:
        self._store = store
        self._terminator = terminator or UnavailableRtcSessionTerminator()

    # ---- pairing ----

    def create_pairing_code(self, owner_id: str, platform: str,
                            device_name_hint: str | None = None,
                            ttl_seconds: int = PAIRING_TTL_SECONDS,
                            now: float | None = None) -> tuple[str, dict]:
        """生成一次性配对码：明文只返回一次，库中只存哈希；TTL<=300"""
        if not 1 <= ttl_seconds <= PAIRING_TTL_SECONDS:
            raise ValueError("pairing TTL 必须在 (0, 300] 秒内")
        return self._store.create_pairing_code(owner_id, platform, ttl_seconds,
                                               now=now)

    # ---- register ----

    def register_device(self, pairing_code: str, device_name: str, platform: str,
                        now: float | None = None) -> DeviceRegistration:
        """原子消费配对码并注册设备；credential_secret 只返回一次"""
        ts = time.time() if now is None else now
        device_id = str(uuid.uuid4())
        credential_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(32)
        expires_at = ts + CREDENTIAL_TTL_DAYS * 86400
        ok = self._store.register_device_from_pairing(
            pairing_code=pairing_code,
            device_id=device_id,
            credential_id=credential_id,
            device_name=device_name,
            platform=platform,
            secret=secret,
            expires_at=expires_at,
            now=ts,
        )
        if not ok:
            raise PairingInvalidError()
        return DeviceRegistration(
            device_id=device_id,
            credential_id=credential_id,
            credential_secret=secret,
            expires_at=expires_at,
        )

    # ---- list ----

    def list_devices(self) -> list[dict]:
        """设备列表：绝不返回 credential_secret / credential_hash"""
        rows = self._store.list_devices()
        return [
            {
                "device_id": row.device_id,
                "device_name": row.device_name,
                "platform": row.platform,
                "status": row.status,
                "expires_at": row.expires_at,
                "last_seen_at": row.last_seen_at,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    # ---- session 指纹登记（签发时调用） ----

    def record_session_issued(self, session_id: str, device_id: str, user_sig: str,
                              expires_at: float, now: float | None = None) -> None:
        """签发时登记 userSig 指纹（哈希），撤销时用于回填 revoked_sessions"""
        fingerprint = hashlib.sha256(user_sig.encode("utf-8")).hexdigest()
        self._store.write_session_event(
            session_id=session_id,
            device_id=device_id,
            event_type="issued",
            state="IN_ROOM",
            metadata={
                "user_sig_fingerprint": fingerprint,
                "user_sig_expires_at": float(expires_at),
            },
            now=now,
        )

    def _active_sessions(self, device_id: str, now: float | None = None) -> list[dict]:
        """未过期活动 session：有 issued 事件且无 terminated 事件，userSig 未过期"""
        ts = time.time() if now is None else now
        events = self._store.list_session_events(device_id, limit=200)
        terminated = {e["session_id"] for e in events if e["event_type"] == "terminated"}
        active: dict[str, dict] = {}
        for event in events:
            if event["event_type"] not in ("issued", "sidecar_sign", "stream_ready"):
                continue
            if event["session_id"] in terminated:
                continue
            meta = json.loads(event["metadata_json"] or "{}")
            expires_at = meta.get("user_sig_expires_at", 0)
            if not expires_at or expires_at <= ts:
                continue  # 未过期才登记
            active[event["session_id"]] = {
                "session_id": event["session_id"],
                "fingerprint": meta.get("user_sig_fingerprint", ""),
                "expires_at": float(expires_at),
            }
        return list(active.values())

    # ---- revoke ----

    def revoke_device(self, device_id: str, reason: str,
                      now: float | None = None) -> dict:
        """强一致撤销：DB 事务内失效 credential + 登记指纹 + 终止事件 + 审计，
        然后尝试外部进程终止；外部失败 → 50301 明确错误（可重试）。"""
        sessions = self._active_sessions(device_id, now=now)
        row = self._store.get_device(device_id)
        if row is None:
            raise DeviceNotFoundError()
        # Credential is invalidated before waiting on the external RTC coordinator.
        self._store.revoke_device(device_id, reason, now=now)
        try:
            confirmed = self.terminate_device_sessions(
                device_id, [session["session_id"] for session in sessions]
            )
        except Exception:  # noqa: BLE001 - 跨进程终止失败不得伪装成功
            raise RevokeTerminationError() from None
        if set(confirmed) != {session["session_id"] for session in sessions}:
            raise RevokeTerminationError()
        return self._store.record_revoke_confirmation(
            device_id, reason, sessions, now=now
        )

    def terminate_device_sessions(self, device_id: str,
                                  session_ids: list[str]) -> list[str]:
        """Delegate to the injected RTC coordinator and wait for exit confirmation."""
        return self._terminator.terminate_and_wait(device_id, session_ids)
