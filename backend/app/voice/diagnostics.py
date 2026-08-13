"""脱敏诊断导出服务（SPEC §4.20 / AC-18 / ADR-018）

- 诊断字段 allowlist（非 denylist）：非允许字段一律丢弃
- 导出文件做敏感键值扫描：命中任一标记即拒绝写出（fail-closed）
- 导出只写入用户指定路径
"""
from __future__ import annotations

import json
from pathlib import Path

# 字段 allowlist（SPEC §4.20 可观测字段；不含任何凭证/内容字段）
DIAGNOSTIC_ALLOWLIST = frozenset({
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
})

# 敏感值扫描标记（denylist 只用于扫描，不作为导出允许项）
SENSITIVE_MARKERS = (
    "credential_secret",
    "credential_hash",
    "user_sig",
    "usersig",
    "nonce",
    "pairing_code",
    "raw_audio",
    "audio_b64",
    "screenshot",
    "transcript",
    "secret_key",
    "secretkey",
    "secret",
    "password",
    "passphrase",
    "private_key",
)


class DiagnosticLeakError(ValueError):
    """导出内容命中敏感标记：拒绝写出"""


def build_redacted_diagnostic(source: dict) -> dict:
    """字段 allowlist 过滤：只保留允许字段"""
    return {key: value for key, value in source.items() if key in DIAGNOSTIC_ALLOWLIST}


def scan_sensitive(text: str) -> list[str]:
    """对导出文本做敏感键值扫描，返回命中标记（空 = 无泄漏）"""
    lowered = text.lower()
    return [marker for marker in SENSITIVE_MARKERS if marker in lowered]


def export_redacted(source: dict, destination: str | Path) -> dict:
    """导出脱敏诊断到用户指定路径；泄漏时抛 DiagnosticLeakError 且不写文件"""
    payload = build_redacted_diagnostic(source)
    text = json.dumps(payload, ensure_ascii=False)
    hits = scan_sensitive(text)
    if hits:
        raise DiagnosticLeakError(f"诊断导出命中敏感标记: {hits}")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return {"path": str(destination), "fields": len(payload)}
