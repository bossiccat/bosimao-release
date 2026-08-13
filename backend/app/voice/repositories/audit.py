"""privacy_audit_events 仓库：只写脱敏审计，禁止敏感键/值。"""
from __future__ import annotations

import json

from .common import assert_redacted_json, now_unix


class AuditRepository:
    def __init__(self, connect):
        self._connect = connect

    def write(self, action: str, subject_type: str, subject_id: str, result: str,
              metadata_redacted_json: dict, now: float | None = None) -> None:
        """写审计事件；metadata 必须通过脱敏白名单校验，否则拒绝写入"""
        assert_redacted_json(metadata_redacted_json)
        ts = now_unix(now)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO privacy_audit_events"
                " (action, subject_type, subject_id, result, metadata_redacted_json,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action, subject_type, subject_id, result,
                 json.dumps(metadata_redacted_json, ensure_ascii=False), ts, ts),
            )
