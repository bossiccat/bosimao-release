"""Atomic KWS wake consumption ledger operations."""
from __future__ import annotations

import time
from typing import Any

from .control_plane_base import InvalidTerminationState
from .timefmt import as_epoch, epoch_to_iso8601
from .user_sig_cipher import (
    UserSigCipher,
    UserSigCipherKeyError,
    UserSigCiphertextMissing,
)

class WakeLedgerMixin:
    def consume_wake(self, *, session_id: str, device_id: str,
                     prior_session_id: str, prior_generation: int,
                     wake_event_id: str, user_sig: str, expires_at: float | int | str,
                     detected_at: str = "", kws_instance_id: str = "",
                     user_id: str = "") -> dict[str, Any]:
        payload = {
            "device_id": device_id, "prior_session_id": prior_session_id,
            "prior_generation": prior_generation, "wake_event_id": wake_event_id,
            "detected_at": detected_at, "kws_instance_id": kws_instance_id,
        }
        payload_hash = self._payload_hash(payload)
        # fail-closed：没有注入 UserSigCipher 就不允许签发（绝不退回明文落库）。
        cipher = self.user_sig_cipher
        if not isinstance(cipher, UserSigCipher):
            raise UserSigCipherKeyError("wake ledger requires an injected user_sig cipher")
        ph = self.ph
        # 云端所有时间列都是 timestamptz（SQLite → Unix float，PG → aware datetime），
        # record_json 是 jsonb。expires_at 先归一成 epoch，再过方言。
        now = self.dialect.timestamp_to_storage(time.time())
        expires_ts = self.dialect.timestamp_to_storage(as_epoch(expires_at))
        with self._txn() as conn:
            replay = conn.execute(
                "SELECT record_json, payload_hash, new_session_id, new_generation,"
                " user_sig_ciphertext, user_sig_encryption_version"
                " FROM control_plane_wake_events"
                f" WHERE device_id = {ph} AND prior_session_id = {ph}"
                f" AND prior_generation = {ph} AND wake_event_id = {ph}",
                (device_id, prior_session_id, prior_generation, wake_event_id),
            ).fetchone()
            if replay is not None:
                if replay["payload_hash"] != payload_hash:
                    raise InvalidTerminationState(
                        "wake event replayed with different payload", code=40913
                    )
                record = self.dialect.json_from_storage(replay["record_json"])
                record["user_sig"] = self._decrypt_stored_user_sig(
                    replay, cipher, device_id=device_id,
                    prior_session_id=prior_session_id,
                    prior_generation=prior_generation,
                    wake_event_id=wake_event_id,
                )
                return record
            prior = conn.execute(
                f"SELECT * FROM control_plane_sessions WHERE session_id = {ph}",
                (prior_session_id,),
            ).fetchone()
            if prior is None:
                raise InvalidTerminationState("prior session not found", code=40402)
            if prior["device_id"] != device_id or prior["generation"] != prior_generation:
                raise InvalidTerminationState("prior session context mismatch", code=40917)
            if prior["state"] != "KWS_READY":
                raise InvalidTerminationState("prior session is not KWS_READY", code=40917)
            active = conn.execute(
                "SELECT COUNT(*) FROM control_plane_sessions"
                f" WHERE device_id = {ph}"
                " AND state IN ('ACTIVE', 'TERMINATING', 'TERMINATION_PARTIAL',"
                " 'TERMINATION_TIMEOUT', 'ENTERING')", (device_id,),
            ).fetchone()[0]
            if active:
                raise InvalidTerminationState(
                    "device already has an active session", code=40917
                )
            generation = prior_generation + 1
            room_id = f"wake-{session_id}"
            conn.execute(
                "UPDATE control_plane_sessions SET state = 'SIGNING',"
                f" updated_at = {ph}"
                f" WHERE session_id = {ph} AND state = 'KWS_READY'",
                (now, prior_session_id),
            )
            conn.execute(
                "INSERT INTO control_plane_sessions"
                " (session_id, device_id, room_id, generation, state, created_at, updated_at)"
                f" VALUES ({ph}, {ph}, {ph}, {ph}, 'SIGNING', {ph}, {ph})",
                (session_id, device_id, room_id, generation, now, now),
            )
            # 同事务入队 pending claim：sidecar 轮询 /session/pending 领取后
            # 才能走 /session/sign → hello proof → redeem 链。claim 生命周期
            # 与本代 user_sig 一致（wake 路由传入的 expires_at）。
            conn.execute(
                "INSERT INTO pending_session_claims"
                " (session_id, device_id, room_id, generation, expires_at,"
                " created_at, updated_at)"
                f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                (session_id, device_id, room_id, generation, expires_ts, now, now),
            )
            # user_sig 只存在于内存返回值里，**不进入 record_json**（防止明文落库）。
            record = {
                "wake_event_id": wake_event_id, "prior_session_id": prior_session_id,
                "prior_generation": prior_generation, "session_id": session_id,
                "device_id": device_id, "room_id": room_id, "user_id": user_id,
                # OpenAPI WakeSessionData.expires_at 声明 date-time；统一按
                # ISO8601 持久化，重放路径返回的也是同一 ISO 字符串
                "expires_at": epoch_to_iso8601(as_epoch(expires_at)),
                "generation": generation, "state": "SIGNING",
            }
            # 密文 + 版本号与同一事务写入：PG 侧两列是 NOT NULL，缺一即写入失败。
            ciphertext = cipher.encrypt(
                user_sig, device_id=device_id, prior_session_id=prior_session_id,
                new_session_id=session_id, prior_generation=prior_generation,
                new_generation=generation, wake_event_id=wake_event_id,
            )
            conn.execute(
                "INSERT INTO control_plane_wake_events"
                " (wake_event_id, device_id, prior_session_id, prior_generation,"
                " new_session_id, new_generation, payload_hash, record_json,"
                " user_sig_ciphertext, user_sig_encryption_version,"
                " consumed_at, created_at, updated_at)"
                f" VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph},"
                f" {ph}, {ph}, {ph}, {ph}, {ph})",
                (wake_event_id, device_id, prior_session_id, prior_generation,
                 session_id, generation, payload_hash,
                 self.dialect.json_to_storage(record, sort_keys=True),
                 ciphertext, cipher.version, now, now, now),
            )
        record["user_sig"] = user_sig
        return record

    @staticmethod
    def _decrypt_stored_user_sig(row: Any, cipher: UserSigCipher, *, device_id: str,
                                 prior_session_id: str, prior_generation: int,
                                 wake_event_id: str) -> str:
        """幂等重放：从密文还原 user_sig。缺密文/缺版本号一律 fail-closed。"""
        blob = row["user_sig_ciphertext"]
        version = row["user_sig_encryption_version"]
        if not isinstance(blob, (bytes, bytearray, memoryview)) or not bytes(blob):
            raise UserSigCiphertextMissing(
                "wake event has no stored user_sig ciphertext"
            )
        if not isinstance(version, str) or not version.strip():
            raise UserSigCiphertextMissing(
                "wake event has no user_sig encryption version"
            )
        return cipher.decrypt(
            blob, device_id=device_id, prior_session_id=prior_session_id,
            new_session_id=row["new_session_id"],
            prior_generation=prior_generation,
            new_generation=row["new_generation"],
            wake_event_id=wake_event_id,
        )

    def replay_wake_payload_mismatch(self, *, device_id: str, prior_session_id: str,
                                     prior_generation: int, wake_event_id: str,
                                     payload: dict[str, Any]) -> bool:
        ph = self.ph
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT payload_hash FROM control_plane_wake_events"
                f" WHERE device_id = {ph} AND prior_session_id = {ph}"
                f" AND prior_generation = {ph} AND wake_event_id = {ph}",
                (device_id, prior_session_id, prior_generation, wake_event_id),
            ).fetchone()
        return row is not None and row["payload_hash"] != self._payload_hash(payload)
