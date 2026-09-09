"""Task B：wake userSig 加密落库契约测试。

背景（对照已应用到云端的 PG 迁移 cloudbase/migrations/20260908154838_voice_control_plane.sql:241-259）：
control_plane_wake_events 有 `user_sig_ciphertext bytea NOT NULL` 与
`user_sig_encryption_version text NOT NULL` 两列，而
`app/voice/control_plane_wake.py` 的 consume_wake() 既把明文 user_sig 写进
record_json（泄漏），又完全不写这两列（上 PG 后 NOT NULL 直接写入失败）。

本文件钉死修复后的行为：
- record_json 落库内容不得含明文 userSig；
- 两列必须真实写入且非空（ciphertext 是 bytes、version 非空 str）；
- 首次返回与重放返回必须是同一个 user_sig（业务契约不变，明文只在内存里）；
- AAD 不匹配 / 密文篡改 / 未知版本 / 错误密钥 / 缺密钥 → 一律 fail-closed 抛异常，
  绝不返回空串或脏数据。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.voice.control_plane import SessionLedger
from app.voice.storage import VoiceStore
from app.voice.user_sig_cipher import (
    CIPHER_VERSION,
    UserSigCipher,
    UserSigCipherError,
    UserSigCipherKeyError,
    UserSigDecryptionError,
)

ACK_NAMES = (
    "android_trtc_left",
    "sidecar_trtc_left",
    "bridge_drained_closed",
    "apm_cancelled_closed",
    "brain_turns_sealed",
)
ACK_REPORTERS_BY_NAME = {
    "android_trtc_left": ("android",),
    "sidecar_trtc_left": ("sidecar",),
    "bridge_drained_closed": ("sidecar", "rtc_bridge"),
    "apm_cancelled_closed": ("rtc_bridge",),
    "brain_turns_sealed": ("brain",),
}

# 32 字节测试密钥（AES-256-GCM）。禁止使用全零/固定弱密钥默认值。
TEST_KEY = hashlib.sha256(b"task-b-user-sig-cipher-test-key").digest()
OTHER_KEY = hashlib.sha256(b"task-b-user-sig-cipher-other-key").digest()
USER_SIG = "eJyrVgrxCdYrSy1Sweek-PLAINTEXT-USER-SIG-DO-NOT-PERSIST"

# 密文 wire format 契约：magic(4) + version(1) + nonce(12) + GCM ciphertext+tag
VERSION_BYTE_OFFSET = 4


def _cipher(key: bytes = TEST_KEY) -> UserSigCipher:
    return UserSigCipher(key)


def _ledger_factory(tmp_path: Path, *, key: bytes = TEST_KEY) -> SessionLedger:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    return SessionLedger(store, user_sig_cipher=_cipher(key))


def _session(ledger: Any, *, generation: int = 7) -> dict[str, Any]:
    value = {
        "session_id": str(uuid.uuid4()),
        "device_id": str(uuid.uuid4()),
        "room_id": f"room-{uuid.uuid4()}",
        "generation": generation,
    }
    ledger.create_session(**value)
    return value


def _kws_ready_session(ledger: Any, *, generation: int = 7) -> dict[str, Any]:
    """把会话完整走完 termination + 所有 ack，进入 KWS_READY（wake 前置条件）。"""
    session = _session(ledger, generation=generation)
    payload = {
        "session_id": session["session_id"],
        "device_id": session["device_id"],
        "room_id": session["room_id"],
        "generation": session["generation"],
        "reason": "user_stop",
    }
    termination = ledger.begin_termination(
        session_id=session["session_id"],
        generation=session["generation"],
        request_id=str(uuid.uuid4()),
        payload=payload,
    )
    termination_id = termination["termination_id"]
    for name in ACK_NAMES:
        for reporter in ACK_REPORTERS_BY_NAME[name]:
            ledger.record_ack(
                termination_id=termination_id,
                session_id=session["session_id"],
                device_id=session["device_id"],
                room_id=session["room_id"],
                generation=session["generation"],
                acknowledgement=name,
                reporter=reporter,
                result="confirmed",
            )
    assert ledger.mark_kws_ready(session["session_id"], generation) is True
    return session


def _wake_kwargs(session: dict[str, Any], wake_event_id: str,
                 *, user_sig: str = USER_SIG,
                 expires_at: float = 4102444800.0) -> dict[str, Any]:
    return dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        prior_generation=session["generation"],
        wake_event_id=wake_event_id,
        user_sig=user_sig,
        expires_at=expires_at,
    )


def _stored_row(db_path: Path) -> sqlite3.Row | None:
    """直连 SQLite 读真实落库内容（绕开应用层对象）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT wake_event_id, new_session_id, new_generation, record_json,"
            " user_sig_ciphertext, user_sig_encryption_version"
            " FROM control_plane_wake_events"
        ).fetchone()
    finally:
        conn.close()


def _count_wake_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM control_plane_wake_events"
        ).fetchone()[0]
    finally:
        conn.close()


def _first_wake(tmp_path: Path, **kwargs: Any) -> tuple[dict[str, Any], Any, str]:
    """执行一次 consume_wake，返回 (record, 会话, db 路径)。"""
    ledger = _ledger_factory(tmp_path, **kwargs)
    session = _kws_ready_session(ledger)
    record = ledger.consume_wake(
        session_id=str(uuid.uuid4()),
        **_wake_kwargs(session, str(uuid.uuid4())),
    )
    return record, session, str(tmp_path / "voice.db")


def _aad(record: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """从一次真实的 wake 结果取出 decrypt 所需的 AAD 绑定项。"""
    return dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        new_session_id=record["session_id"],
        prior_generation=record["prior_generation"],
        new_generation=record["generation"],
        wake_event_id=record["wake_event_id"],
    )


# ---- 1) record_json 不得含明文 userSig ----


def test_record_json_column_has_no_plaintext_user_sig(tmp_path: Path) -> None:
    _, _, db_path = _first_wake(tmp_path)
    row = _stored_row(Path(db_path))
    assert row is not None

    raw = row["record_json"]
    assert isinstance(raw, str) and raw
    assert USER_SIG not in raw, "record_json 泄漏了明文 userSig"
    assert "user_sig" not in raw, "record_json 仍带 user_sig 字段"

    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert "user_sig" not in parsed, "落库 JSON 反序列化后仍含 user_sig"


# ---- 2) 两列必须真实写入且非空 ----


def test_ciphertext_and_version_columns_are_persisted(tmp_path: Path) -> None:
    _, _, db_path = _first_wake(tmp_path)
    row = _stored_row(Path(db_path))
    assert row is not None

    ciphertext = row["user_sig_ciphertext"]
    version = row["user_sig_encryption_version"]

    assert isinstance(ciphertext, bytes), type(ciphertext)
    assert len(ciphertext) > 0
    assert isinstance(version, str)
    assert version.strip() != ""
    assert version == CIPHER_VERSION


# ---- 3) 首次返回 == 重放返回 ----


def test_replay_returns_identical_user_sig_as_first_call(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    wake_event_id = str(uuid.uuid4())

    first = ledger.consume_wake(session_id=str(uuid.uuid4()),
                                **_wake_kwargs(session, wake_event_id))
    replay = ledger.consume_wake(session_id=str(uuid.uuid4()),
                                 **_wake_kwargs(session, wake_event_id))

    assert first["user_sig"] == USER_SIG
    assert replay["user_sig"] == USER_SIG
    assert replay == first


def test_replay_across_rebuilt_ledger_returns_identical_user_sig(tmp_path: Path) -> None:
    ledger = _ledger_factory(tmp_path)
    session = _kws_ready_session(ledger)
    wake_event_id = str(uuid.uuid4())
    first = ledger.consume_wake(session_id=str(uuid.uuid4()),
                                **_wake_kwargs(session, wake_event_id))

    rebuilt = _ledger_factory(tmp_path)
    replay = rebuilt.consume_wake(session_id=str(uuid.uuid4()),
                                  **_wake_kwargs(session, wake_event_id))

    assert replay == first
    assert replay["user_sig"] == first["user_sig"] == USER_SIG


# ---- 4) fail-closed ----


@pytest.mark.parametrize("field", [
    "device_id", "prior_session_id", "new_session_id",
    "prior_generation", "new_generation", "wake_event_id",
])
def test_decrypt_rejects_mismatched_aad(tmp_path: Path, field: str) -> None:
    _, session, db_path = _first_wake(tmp_path)
    row = _stored_row(Path(db_path))
    assert row is not None
    aad = dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        new_session_id=row["new_session_id"],
        prior_generation=session["generation"],
        new_generation=row["new_generation"],
        wake_event_id=row["wake_event_id"],
    )
    tampered = dict(aad)
    tampered[field] = str(uuid.uuid4()) if isinstance(aad[field], str) else aad[field] + 1

    with pytest.raises(UserSigDecryptionError):
        _cipher().decrypt(row["user_sig_ciphertext"], **tampered)


def test_decrypt_rejects_tampered_ciphertext(tmp_path: Path) -> None:
    _, session, db_path = _first_wake(tmp_path)
    row = _stored_row(Path(db_path))
    assert row is not None
    aad = dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        new_session_id=row["new_session_id"],
        prior_generation=session["generation"],
        new_generation=row["new_generation"],
        wake_event_id=row["wake_event_id"],
    )

    blob = bytearray(row["user_sig_ciphertext"])
    blob[-1] ^= 0x01  # 翻转密文最后一字节（GCM tag 必炸）
    flipped = bytes(blob)

    with pytest.raises(UserSigDecryptionError):
        _cipher().decrypt(flipped, **aad)
    # 绝不能返回空串/None 蒙混过关：必须抛异常，异常信息不得含明文
    try:
        _cipher().decrypt(flipped, **aad)
    except UserSigCipherError as exc:
        assert USER_SIG not in str(exc)
    else:  # pragma: no cover - 到这里说明没有 fail-closed
        pytest.fail("篡改密文后没有 fail-closed")


def test_decrypt_rejects_unknown_version(tmp_path: Path) -> None:
    _, session, db_path = _first_wake(tmp_path)
    row = _stored_row(Path(db_path))
    assert row is not None
    aad = dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        new_session_id=row["new_session_id"],
        prior_generation=session["generation"],
        new_generation=row["new_generation"],
        wake_event_id=row["wake_event_id"],
    )

    blob = bytes(row["user_sig_ciphertext"])
    unknown = blob[:VERSION_BYTE_OFFSET] + b"\x7f" + blob[VERSION_BYTE_OFFSET + 1:]

    with pytest.raises(UserSigDecryptionError):
        _cipher().decrypt(unknown, **aad)


def test_decrypt_rejects_wrong_key(tmp_path: Path) -> None:
    _, session, db_path = _first_wake(tmp_path)
    row = _stored_row(Path(db_path))
    assert row is not None
    aad = dict(
        device_id=session["device_id"],
        prior_session_id=session["session_id"],
        new_session_id=row["new_session_id"],
        prior_generation=session["generation"],
        new_generation=row["new_generation"],
        wake_event_id=row["wake_event_id"],
    )

    with pytest.raises(UserSigDecryptionError):
        _cipher(OTHER_KEY).decrypt(row["user_sig_ciphertext"], **aad)


@pytest.mark.parametrize("bad_key", [b"", b"short", b"x" * 31, None])
def test_cipher_construction_is_fail_closed_without_valid_key(bad_key: Any) -> None:
    with pytest.raises(UserSigCipherKeyError):
        UserSigCipher(bad_key)


def test_ledger_without_injected_cipher_is_fail_closed(tmp_path: Path) -> None:
    """未注入 cipher 时 consume_wake 必须失败，绝不能退化成明文落库。"""
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    ledger = SessionLedger(store)
    session = _kws_ready_session(ledger)

    with pytest.raises(UserSigCipherError):
        ledger.consume_wake(session_id=str(uuid.uuid4()),
                            **_wake_kwargs(session, str(uuid.uuid4())))

    assert _count_wake_rows(tmp_path / "voice.db") == 0, "fail-closed 前不得落任何脏数据"
