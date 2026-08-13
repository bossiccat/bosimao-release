"""VoiceStore SQLite 安全存储验收测试（QA spec §5.2 强制行为）

全部使用临时真实 SQLite 文件 + 正式迁移；禁止内存 dict / fake repository。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from app.voice.storage import VoiceStore  # noqa: E402

BUSINESS_TABLES = {
    "settings",
    "device_credentials",
    "pairing_codes",
    "revoked_sessions",
    "session_events",
    "transcripts",
    "privacy_audit_events",
    "consumed_nonces",
    "rate_limit_buckets",
}


def _scan_for(store: VoiceStore, secrets: list[str]) -> list[str]:
    """对真实数据库文件做明文 bytes 扫描，返回命中的敏感明文。"""
    raw = Path(store.db_path).read_bytes()
    hits: list[str] = []
    for secret in secrets:
        if secret.encode("latin1", errors="ignore") in raw:
            hits.append(secret)
    return hits


def test_migrations_create_all_nine_tables_and_schema_migrations(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = {row[0] for row in rows}
    assert BUSINESS_TABLES <= tables
    assert "schema_migrations" in tables


def test_key_unique_and_indexes_match_spec(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    with store.connect() as conn:
        device_unique_indexes = conn.execute(
            "SELECT name FROM pragma_index_list('device_credentials') WHERE \"unique\"=1"
        ).fetchall()
        device_unique_columns = {
            tuple(
                row[2]
                for row in conn.execute(
                    "SELECT * FROM pragma_index_info(?)", (index_name,)
                ).fetchall()
            )
            for (index_name,) in device_unique_indexes
        }
        assert ("device_id",) in device_unique_columns
        assert ("credential_id",) in device_unique_columns
        code_unique = conn.execute(
            "SELECT COUNT(*) FROM pragma_index_list('pairing_codes') WHERE \"unique\"=1"
        ).fetchone()[0]
        assert code_unique == 1
        revoked_unique = conn.execute(
            "SELECT COUNT(*) FROM pragma_index_list('revoked_sessions') WHERE \"unique\"=1"
        ).fetchone()[0]
        assert revoked_unique == 1
        nonce_unique = conn.execute(
            "SELECT COUNT(*) FROM pragma_index_list('consumed_nonces') WHERE \"unique\"=1"
        ).fetchone()[0]
        assert nonce_unique == 1
        rate_unique = conn.execute(
            "SELECT COUNT(*) FROM pragma_index_list('rate_limit_buckets') WHERE \"unique\"=1"
        ).fetchone()[0]
        assert rate_unique == 1


def test_transaction_rollback_leaves_no_partial_write(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO settings(key, value_encrypted, created_at, updated_at)"
                    " VALUES (?, ?, 1.0, 1.0)",
                    ("first", "x"),
                )
                conn.execute(
                    "INSERT INTO settings(key, value_encrypted, created_at, updated_at)"
                    " VALUES (?, ?, 2.0, 2.0)",
                    ("first", "duplicate"),
                )
        rows = conn.execute("SELECT key FROM settings").fetchall()
    assert rows == []


def test_pairing_code_only_stored_as_hash(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    code, meta = store.create_pairing_code(owner_id="owner-1", platform="android", ttl_seconds=120)
    assert meta["max_uses"] == 1
    assert meta["ttl_seconds"] == 120
    with store.connect() as conn:
        row = conn.execute(
            "SELECT code_hash FROM pairing_codes WHERE created_by_owner_id=?",
            ("owner-1",),
        ).fetchone()
    assert row is not None
    assert row[0] != code
    assert row[0] == hashlib.sha256(code.encode()).hexdigest()
    assert _scan_for(store, [code]) == []


def test_pairing_ttl_is_bounded_exclusive_positive(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    for ttl in (1, 300):
        code, meta = store.create_pairing_code(owner_id=f"owner-{ttl}", platform="android", ttl_seconds=ttl)
        assert meta["ttl_seconds"] == ttl
        with store.connect() as conn:
            created, expires = conn.execute(
                "SELECT created_at, expires_at FROM pairing_codes WHERE code_hash=?",
                (hashlib.sha256(code.encode()).hexdigest(),),
            ).fetchone()
        ttl_s = expires - created
        assert 0 < ttl_s <= 300
    for bad_ttl in (0, -1, 301, 600):
        with pytest.raises(ValueError):
            store.create_pairing_code(owner_id=f"owner-bad-{bad_ttl}", platform="android", ttl_seconds=bad_ttl)


def test_consume_pairing_code_atomic_once(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    code, _ = store.create_pairing_code(owner_id="owner-1", platform="android", ttl_seconds=120)
    device_id = uuid.uuid4().hex
    ok = store.consume_pairing_code(code, device_id=device_id)
    assert ok is True
    with store.connect() as conn:
        row = conn.execute(
            "SELECT consumed_device_id, consumed_at FROM pairing_codes WHERE code_hash=?",
            (hashlib.sha256(code.encode()).hexdigest(),),
        ).fetchone()
    assert row[0] == device_id
    assert row[1] is not None
    assert store.consume_pairing_code(code, device_id=uuid.uuid4().hex) is False


def test_expired_pairing_code_rejected_and_no_device_created(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    code, meta = store.create_pairing_code(
        owner_id="owner-1", platform="android", ttl_seconds=1, now=time.time() - 120
    )
    assert meta["ttl_seconds"] == 1
    ok = store.consume_pairing_code(code, device_id=uuid.uuid4().hex, now=time.time())
    assert ok is False
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]
    assert count == 0


def test_concurrent_pairing_consumption_exactly_one_winner(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    code, _ = store.create_pairing_code(owner_id="owner-1", platform="android", ttl_seconds=120)
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(6)

    def consume(device_id: str) -> None:
        barrier.wait()
        ok = store.consume_pairing_code(code, device_id=device_id)
        if ok:
            store.save_device(device_id=device_id, secret="winner-secret")
        with results_lock:
            results.append(ok)

    threads = [
        threading.Thread(target=consume, args=(uuid.uuid4().hex,)) for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    with store.connect() as conn:
        consumed = conn.execute(
            "SELECT COUNT(*) FROM pairing_codes WHERE consumed_at IS NOT NULL"
        ).fetchone()[0]
        devices = conn.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]
    assert consumed == 1
    assert devices == 1


def test_device_secret_is_hashed_and_never_returned_from_storage(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(device_id="phone-1", secret="plain-secret")
    row = store.get_device("phone-1")
    assert row.credential_hash != "plain-secret"
    assert "plain-secret" not in row.credential_hash
    salt, digest = row.credential_hash.split("$", 1)
    assert hashlib.sha256((salt + "plain-secret").encode()).hexdigest() == digest
    assert _scan_for(store, ["plain-secret"]) == []


def test_nonce_is_subject_bound_and_replay_rejected(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    nonce = uuid.uuid4().hex
    assert store.consume_nonce(subject_id="device-a", nonce=nonce) is True
    assert store.consume_nonce(subject_id="device-a", nonce=nonce) is False
    assert store.consume_nonce(subject_id="device-b", nonce=nonce) is True


def test_nonce_expiry_cleanup_keeps_fresh_records(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    now = time.time()
    store.consume_nonce(subject_id="expired", nonce="old-nonce", now=now - 2000, ttl_seconds=300)
    store.consume_nonce(subject_id="fresh", nonce="new-nonce", now=now, ttl_seconds=300)
    store.purge_expired_nonces(now=now)
    with store.connect() as conn:
        rows = conn.execute("SELECT subject_id FROM consumed_nonces").fetchall()
    assert {row[0] for row in rows} == {"fresh"}


def test_sensitive_values_rejected_in_audit_and_events(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    sensitive = {
        "secret": "credential_secret_value",
        "nonce": "raw-nonce-value",
        "user_sig": "userSigRawValue",
        "audio": "raw-audio-bytes",
        "transcript": "full transcript text",
    }
    with pytest.raises(ValueError):
        store.write_audit(
            action="device.register",
            subject_type="device",
            subject_id="d1",
            result="ok",
            metadata_redacted_json={"secret": sensitive["secret"]},
        )
    with pytest.raises(ValueError):
        store.write_audit(
            action="device.register",
            subject_type="device",
            subject_id="d1",
            result="ok",
            metadata_redacted_json={"user_sig": sensitive["user_sig"]},
        )
    with pytest.raises(ValueError):
        store.write_audit(
            action="device.register",
            subject_type="device",
            subject_id="d1",
            result="ok",
            metadata_redacted_json={"transcript": sensitive["transcript"]},
        )
    with pytest.raises(ValueError):
        store.write_audit(
            action="device.register",
            subject_type="device",
            subject_id="d1",
            result="ok",
            metadata_redacted_json={"pairing_code": "raw-pairing-code"},
        )
    with pytest.raises(ValueError):
        store.write_audit(
            action="device.register",
            subject_type="device",
            subject_id="d1",
            result="ok",
            metadata_redacted_json={"credential_secret": "credential_secret_value"},
        )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT metadata_redacted_json FROM privacy_audit_events WHERE subject_id='d1'"
        ).fetchone()
    assert row is None
    assert _scan_for(store, list(sensitive.values())) == []
