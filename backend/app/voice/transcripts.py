"""加密转写存储服务（SPEC §9.2 / AC-16 / ADR-018）

- 默认不持久化：未开启 transcript_persistence_enabled 时 save 不创建任何正文记录
- 显式开启后以 OS-bound key（Windows DPAPI 或等价适配器）加密保存，只存密文
- 删除不留正文副本：删除后审计不含正文，DB 字节扫描无明文
- 导出仅本地解密后写入用户指定路径，不上传第三方
"""
from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from .storage import VoiceStore

PERSISTENCE_KEY = "privacy:transcript_persistence_enabled"


class OsBoundKeyCipher(ABC):
    """OS-bound key 适配器接口：加密版本 + encrypt/decrypt"""

    encryption_version: str = ""

    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes: ...

    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes: ...


class MemoryKeyCipher(OsBoundKeyCipher):
    """内存 fake（仅供测试/无 OS 绑定开发）：确定性可逆变换，非安全实现"""

    encryption_version = "mem-v1"
    _MASK = 0x5A

    def encrypt(self, plaintext: bytes) -> bytes:
        return b"MEM1:" + bytes(b ^ self._MASK for b in plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return bytes(b ^ self._MASK for b in ciphertext[5:])


class WindowsDpapiCipher(OsBoundKeyCipher):
    """Windows DPAPI（CryptProtectData，当前用户 OS-bound key）"""

    encryption_version = "dpapi-v1"

    def encrypt(self, plaintext: bytes) -> bytes:
        import win32crypt

        # pywin32 312：CryptProtectData 直接返回 blob bytes
        return win32crypt.CryptProtectData(plaintext, None, None, None, None, 0)

    def decrypt(self, ciphertext: bytes) -> bytes:
        import win32crypt

        # CryptUnprotectData 返回 (description, dataOut)
        return win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1]


class TranscriptService:
    def __init__(self, store: VoiceStore, cipher: OsBoundKeyCipher,
                 persistence_checker=None) -> None:
        self._store = store
        self._cipher = cipher
        self._persistence_checker = persistence_checker or self._default_persistence

    def _default_persistence(self) -> bool:
        raw = self._store.get_setting(PERSISTENCE_KEY)
        return False if raw is None else bool(json.loads(raw))

    # ---- 写入 ----

    def save(self, session_id: str, text: str, now: float | None = None) -> int | None:
        """保存转写；持久化未开启时返回 None 且不创建记录（AC-16）"""
        if not self._persistence_checker():
            return None
        ciphertext = self._cipher.encrypt(text.encode("utf-8"))
        ts = time.time() if now is None else now
        with self._store.connect() as conn:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO transcripts"
                    " (session_id, ciphertext, encryption_version, started_at,"
                    "  created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, ciphertext, self._cipher.encryption_version, ts, ts, ts),
                )
                transcript_id = int(cursor.lastrowid)
        self._store.write_audit(
            "transcript.save", "transcript", str(transcript_id), "ok",
            {"session_id": session_id, "cipher_bytes": len(ciphertext)}, now=ts,
        )
        return transcript_id

    # ---- 读取 ----

    def list(self, limit: int = 50) -> list[dict]:
        """元数据列表（不含正文）"""
        with self._store.connect() as conn:
            rows = conn.execute(
                "SELECT id, session_id, encryption_version, started_at, created_at"
                " FROM transcripts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "transcript_id": row["id"],
                "session_id": row["session_id"],
                "encryption_version": row["encryption_version"],
                "started_at": row["started_at"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get(self, transcript_id: int) -> str | None:
        """解密返回正文；不存在返回 None"""
        with self._store.connect() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM transcripts WHERE id = ?", (transcript_id,)
            ).fetchone()
        if row is None:
            return None
        return self._cipher.decrypt(row[0]).decode("utf-8")

    def get_any_by_session(self, session_id: str) -> str | None:
        with self._store.connect() as conn:
            row = conn.execute(
                "SELECT id, ciphertext FROM transcripts WHERE session_id = ?"
                " ORDER BY created_at DESC LIMIT 1", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return self._cipher.decrypt(row[1]).decode("utf-8")

    # ---- 删除（不留正文副本） ----

    def delete(self, transcript_id: int | None = None, now: float | None = None) -> int:
        """删除单条或全部密文；审计记录不含正文"""
        ts = time.time() if now is None else now
        with self._store.connect() as conn:
            with conn:
                if transcript_id is None:
                    cursor = conn.execute("DELETE FROM transcripts")
                    deleted = cursor.rowcount
                    subject = "*"
                else:
                    cursor = conn.execute("DELETE FROM transcripts WHERE id = ?", (transcript_id,))
                    deleted = cursor.rowcount
                    subject = str(transcript_id)
        self._store.write_audit(
            "transcript.delete", "transcript", subject, "ok",
            {"deleted": deleted}, now=ts,
        )
        return deleted

    # ---- 导出（仅用户指定路径） ----

    def export(self, destination: str | Path, transcript_id: int | None = None,
               create_parents: bool = True) -> int:
        """本地解密后导出到用户指定路径。

        先全部解密到内存（解密失败不落任何文件），成功后才写临时文件并原子
        rename 到目标路径；写失败清理临时文件，不留半文件。
        """
        destination = Path(destination)
        if not create_parents and not destination.parent.exists():
            raise ValueError(f"导出目标目录不存在: {destination.parent}")
        with self._store.connect() as conn:
            if transcript_id is None:
                rows = conn.execute(
                    "SELECT ciphertext FROM transcripts ORDER BY created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ciphertext FROM transcripts WHERE id = ?", (transcript_id,)
                ).fetchall()
        # 解密失败发生在任何文件创建之前
        texts = [self._cipher.decrypt(row[0]).decode("utf-8") for row in rows]
        payload = "\n".join(texts)  # 多条以换行分隔；单条无尾换行
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, destination)  # 原子替换，避免半文件
        except Exception:  # noqa: BLE001
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # noqa: BLE001 - 清理失败不掩盖原始错误
                pass
            raise
        return len(texts)
