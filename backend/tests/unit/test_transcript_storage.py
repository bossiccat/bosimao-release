"""加密转写存储验收测试（SPEC §9.2 / AC-16 / ADR-018）

- 默认无 transcripts 正文：未开启持久化时 save 不创建任何记录
- OS-bound key 适配器接口（Windows DPAPI 真实适配器 + 内存 fake）
- 只存密文：DB 字节扫描无明文正文
- 删除不留正文副本：删除后表/审计/诊断均无正文
- 导出只写到用户指定路径（解密后），不上传第三方
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.voice.privacy import FakeRuntimeActions, PrivacyService
from app.voice.storage import VoiceStore
from app.voice.transcripts import (
    MemoryKeyCipher,
    OsBoundKeyCipher,
    TranscriptService,
    WindowsDpapiCipher,
)

TEXT_A = "今天帮我重构一下登录模块的鉴权逻辑"
TEXT_B = "拆解一下这个卡顿问题，看看是渲染还是 IO"


def _store(tmp_path: Path) -> VoiceStore:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    return store


def _enabled_service(tmp_path: Path, cipher: OsBoundKeyCipher | None = None):
    store = _store(tmp_path)
    privacy = PrivacyService(store, actions=FakeRuntimeActions())
    privacy.set("transcript_persistence_enabled", True)
    svc = TranscriptService(store, cipher=cipher or MemoryKeyCipher())
    return svc, store, privacy


def _scan(db: VoiceStore, secrets: list[str]) -> list[str]:
    raw = Path(db.db_path).read_bytes()
    return [s for s in secrets if s.encode("utf-8") in raw]


def test_default_no_persistence_creates_no_transcript_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    svc = TranscriptService(store, cipher=MemoryKeyCipher())
    result = svc.save("sess-1", TEXT_A)
    assert result is None
    with store.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    assert rows == 0
    assert _scan(store, [TEXT_A]) == []


def test_enabled_persistence_saves_encrypted_only(tmp_path: Path) -> None:
    svc, store, _privacy = _enabled_service(tmp_path)
    transcript_id = svc.save("sess-1", TEXT_A)
    assert transcript_id is not None
    with store.connect() as conn:
        row = conn.execute(
            "SELECT ciphertext, encryption_version FROM transcripts WHERE session_id='sess-1'"
        ).fetchone()
    assert row is not None
    assert row[0] != TEXT_A.encode("utf-8")          # 不是明文
    assert row[1] == MemoryKeyCipher.encryption_version
    assert _scan(store, [TEXT_A]) == []              # DB 字节扫描无明文


def test_list_and_get_roundtrip(tmp_path: Path) -> None:
    svc, store, _privacy = _enabled_service(tmp_path)
    id_a = svc.save("sess-1", TEXT_A)
    id_b = svc.save("sess-2", TEXT_B)
    items = svc.list()
    assert {item["transcript_id"] for item in items} == {id_a, id_b}
    assert svc.get(id_a) == TEXT_A
    assert svc.get(id_b) == TEXT_B


def test_delete_all_removes_rows_and_no_plaintext_copy(tmp_path: Path) -> None:
    svc, store, privacy = _enabled_service(tmp_path)
    svc.save("sess-1", TEXT_A)
    svc.save("sess-2", TEXT_B)
    deleted = svc.delete()
    assert deleted == 2
    with store.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    assert rows == 0
    # 删除后 DB 无正文副本（含审计表）
    assert _scan(store, [TEXT_A, TEXT_B]) == []
    with store.connect() as conn:
        audit = conn.execute(
            "SELECT metadata_redacted_json FROM privacy_audit_events WHERE action='transcript.delete'"
        ).fetchall()
    assert len(audit) >= 1
    audit_text = "".join(row[0] for row in audit)
    assert TEXT_A not in audit_text and TEXT_B not in audit_text


def test_delete_single_transcript(tmp_path: Path) -> None:
    svc, store, _privacy = _enabled_service(tmp_path)
    id_a = svc.save("sess-1", TEXT_A)
    svc.save("sess-2", TEXT_B)
    assert svc.delete(transcript_id=id_a) == 1
    assert svc.get(id_a) is None
    assert svc.get_any_by_session("sess-2") == TEXT_B


def test_export_writes_only_to_destination(tmp_path: Path) -> None:
    svc, store, _privacy = _enabled_service(tmp_path)
    svc.save("sess-1", TEXT_A)
    destination = tmp_path / "export" / "transcript.txt"
    exported = svc.export(destination)
    assert exported == 1
    assert destination.read_text(encoding="utf-8") == TEXT_A
    # 只写用户指定路径：目录内无其他文件
    assert sorted(p.name for p in (tmp_path / "export").iterdir()) == ["transcript.txt"]
    # 导出不是删除：DB 仍有记录
    with store.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    assert rows == 1


def test_export_rejects_invalid_destination(tmp_path: Path) -> None:
    svc, store, _privacy = _enabled_service(tmp_path)
    svc.save("sess-1", TEXT_A)
    bad = tmp_path / "no_such_dir" / "nested" / "out.txt"
    with pytest.raises(ValueError):
        svc.export(bad, create_parents=False)


def test_export_decrypt_failure_leaves_no_file(tmp_path: Path, monkeypatch) -> None:
    """QA advisory：解密失败不得留下空/半文件——先解密成功后才写目标路径"""
    svc, store, _privacy = _enabled_service(tmp_path)
    svc.save("sess-1", TEXT_A)

    def _boom(ciphertext: bytes) -> bytes:
        raise RuntimeError("decrypt failed")

    monkeypatch.setattr(svc._cipher, "decrypt", _boom)
    destination = tmp_path / "out.txt"
    with pytest.raises(RuntimeError):
        svc.export(destination)
    assert not destination.exists()
    # 无残留导出/临时文件（目录内除测试 fixture 的 voice.db* 外无其他文件）
    leftovers = [p.name for p in tmp_path.iterdir()
                 if not p.name.startswith("voice.db")]
    assert leftovers == []


def test_memory_cipher_interface_contract() -> None:
    cipher = MemoryKeyCipher()
    encrypted = cipher.encrypt(TEXT_A.encode("utf-8"))
    assert encrypted != TEXT_A.encode("utf-8")
    assert cipher.decrypt(encrypted) == TEXT_A.encode("utf-8")
    assert cipher.encryption_version


def test_windows_dpapi_cipher_roundtrip() -> None:
    """Windows DPAPI OS-bound key 适配器：真实加密/解密 roundtrip（本机 Windows+pywin32）"""
    cipher = WindowsDpapiCipher()
    assert cipher.encryption_version
    encrypted = cipher.encrypt(TEXT_B.encode("utf-8"))
    assert encrypted != TEXT_B.encode("utf-8")
    assert cipher.decrypt(encrypted) == TEXT_B.encode("utf-8")
