"""脱敏诊断导出验收测试（SPEC §4.20 / §9.2 / AC-18 / ADR-018）

- 诊断采用字段 allowlist（而非 denylist）：非允许字段一律丢弃
- 导出文件敏感键值扫描 0 泄漏（credential/nonce/userSig/原始音频/截图/代码/完整转写）
- 导出只写到用户指定路径
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.voice.diagnostics import (
    DIAGNOSTIC_ALLOWLIST,
    DiagnosticLeakError,
    build_redacted_diagnostic,
    export_redacted,
    scan_sensitive,
)

ALLOWED_KEYS = {
    "session_id",
    "turn_id",
    "state",
    "error_code",
    "error_message",
    "latency_ms",
    "up_frame_count",
    "up_bytes",
    "down_frame_count",
    "down_bytes",
    "first_remote_audio_ts",
    "first_nonzero_playback_ts",
    "queue_depth",
    "queue_high_watermark",
    "queue_drops",
    "backpressure_events",
    "reconnects",
    "sdk_version",
    "model",
    "os_version",
    "app_version",
    "device_id_masked",
    "created_at",
    "ended_at",
    "duration_ms",
}

SENSITIVE_SOURCE = {
    "session_id": "sess-0001",
    "state": "IN_ROOM",
    "error_code": None,
    "latency_ms": 42,
    "up_frame_count": 10,
    "queue_depth": 0,
    "credential_secret": "sk-secret-0123456789abcdef",
    "user_sig": "userSigRawValue",
    "nonce": "raw-nonce-value",
    "raw_audio_b64": "UklGRi4AAABXQVZFZm10IBAAAAABAAEAgD4AAIA+AAABAAgAZGF0YQAAAAA=",
    "screenshot_b64": "iVBORw0KGgoAAAANSUhEUg==",
    "transcript": "完整转写正文内容",
    "code": "def secret_key(): return 123",
    "device_credential": "device-secret-raw",
}


def test_allowlist_only_fields_kept() -> None:
    payload = build_redacted_diagnostic(SENSITIVE_SOURCE)
    assert set(payload) <= DIAGNOSTIC_ALLOWLIST
    assert payload["session_id"] == "sess-0001"
    assert payload["latency_ms"] == 42


def test_sensitive_keys_never_survive_allowlist() -> None:
    payload = build_redacted_diagnostic(SENSITIVE_SOURCE)
    text = json.dumps(payload, ensure_ascii=False).lower()
    for marker in ("credential_secret", "user_sig", "nonce", "raw_audio",
                   "screenshot", "transcript", "device_credential"):
        assert marker not in text, f"泄漏敏感字段: {marker}"
    # 源码值（allowlist 字段 error_code 含 "code" 子串，须按值断言）
    assert "def secret_key" not in text
    assert "sk-secret" not in text
    assert "完整转写正文内容" not in json.dumps(payload, ensure_ascii=False)


def test_sensitive_value_scan_detects_leaks() -> None:
    leaked = {"session_id": "s", "error_message": "auth failed with secret abc123"}
    assert scan_sensitive(json.dumps(leaked))  # 值含 "secret" 标记 → 命中
    clean = {"session_id": "s", "error_message": "auth_failed"}
    assert scan_sensitive(json.dumps(clean)) == []


def test_export_redacted_writes_only_destination_and_scan_clean(tmp_path: Path) -> None:
    destination = tmp_path / "diag" / "voice_diagnostics.json"
    result = export_redacted(SENSITIVE_SOURCE, destination)
    assert result["path"] == str(destination)
    text = destination.read_text(encoding="utf-8")
    assert scan_sensitive(text) == []
    assert "credential_secret" not in text
    assert "userSigRawValue" not in text
    assert "完整转写正文内容" not in text
    assert sorted(p.name for p in (tmp_path / "diag").iterdir()) == ["voice_diagnostics.json"]


def test_export_redacted_rejects_on_leak(tmp_path: Path) -> None:
    destination = tmp_path / "out.json"
    # allowlist 字段的值本身含敏感内容（如 error_message 被污染）→ 必须拒绝写出
    with pytest.raises(DiagnosticLeakError):
        export_redacted(
            {"session_id": "s", "error_message": "failed with credential_secret sk-secret-abc"},
            destination,
        )
    assert not destination.exists()  # 泄漏时不得写出文件


def test_allowlist_is_explicit_contract() -> None:
    # allowlist 必须覆盖 SPEC §4.20 可观测字段且不包含敏感字段
    for key in ALLOWED_KEYS:
        assert key in DIAGNOSTIC_ALLOWLIST
    for key in ("credential_secret", "user_sig", "nonce", "raw_audio_b64",
                "transcript", "code"):
        assert key not in DIAGNOSTIC_ALLOWLIST
