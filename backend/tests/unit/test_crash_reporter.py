"""阶段 E-1 崩溃落盘收集器单元测试

覆盖：
- 敏感值脱敏（key=value / 引号包裹 / bearer / sk- / JWT / 裸 token 值）
- 非敏感文本不被误伤
- 落盘内容：时间戳 + 版本 + 异常类型 + 脱敏消息 + 脱敏 traceback + 请求上下文
- fail-safe：落盘失败不抛异常
- FastAPI 全局异常处理器：记录 method/path 并回统一 500（code=50000）
"""
from __future__ import annotations

import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.utils import crash_reporter as cr

REDACTED = "<REDACTED>"


class TestRedactText:
    def test_key_value_forms(self):
        cases = {
            "voice_token=abc123secret": f"voice_token={REDACTED}",
            "TRTC_SECRETKEY: xyz-456": f"TRTC_SECRETKEY: {REDACTED}",
            'feishu_app_secret = "s3cr3t"': f"feishu_app_secret = {REDACTED}",
            "deepseek_api_key=sk-1234567890": f"deepseek_api_key={REDACTED}",
            "voice_e2ee_key=base64value": f"voice_e2ee_key={REDACTED}",
            '{"token": "abc"}': f'{{"token": {REDACTED}}}',
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig": (
                f"Authorization: {REDACTED}"
            ),
        }
        for raw, expected in cases.items():
            assert cr.redact_text(raw) == expected, f"raw={raw!r}"

    def test_bare_secret_formats(self):
        text = "err: key sk-abcdef123456 and Bearer abcdef1234567890 token abcdef12345678"
        out = cr.redact_text(text)
        assert "sk-abcdef123456" not in out
        assert "abcdef1234567890" not in out
        assert "abcdef12345678" not in out
        assert REDACTED in out

    def test_non_sensitive_untouched(self):
        text = "monkey=5, hockey_score=3, keyboard: us, page=1&limit=20"
        assert cr.redact_text(text) == text


class TestReportCrash:
    def test_writes_redacted_file(self, tmp_path):
        try:
            raise ValueError("auth failed token=secret-abcdef123")
        except ValueError:
            path = cr.report_crash(
                *sys.exc_info(),
                request={"method": "POST", "path": "/api/v1/x"},
                version="1.2.3",
                crash_dir=tmp_path,
            )
        assert path is not None and path.exists()
        raw = path.read_text(encoding="utf-8")
        assert "secret-abcdef123" not in raw
        data = json.loads(raw)
        assert data["exc_type"] == "ValueError"
        assert data["version"] == "1.2.3"
        assert data["ts"]
        assert data["thread"]
        assert data["request"] == {"method": "POST", "path": "/api/v1/x"}
        assert data["exc_message"] == f"auth failed token={REDACTED}"
        assert REDACTED in data["traceback"]

    def test_fail_safe_on_bad_dir(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            # crash_dir 的父级是普通文件 → mkdir 必然失败，须返回 None 而非抛出
            result = cr.report_crash(
                *sys.exc_info(), crash_dir=blocker / "sub"
            )
        assert result is None


class TestFastapiHandler:
    def test_returns_500_and_records_request(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cr, "_CRASH_DIR", tmp_path)
        app = FastAPI()
        app.add_exception_handler(Exception, cr.build_fastapi_exception_handler("9.9.9"))

        @app.get("/boom")
        def boom():
            raise RuntimeError("api key sk-1234567890 leaked")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/boom")
        assert resp.status_code == 500
        assert resp.json()["code"] == 50000
        files = list(tmp_path.glob("crash-*.json"))
        assert len(files) == 1
        raw = files[0].read_text(encoding="utf-8")
        assert "sk-1234567890" not in raw
        data = json.loads(raw)
        assert data["request"] == {"method": "GET", "path": "/boom"}
        assert data["version"] == "9.9.9"
