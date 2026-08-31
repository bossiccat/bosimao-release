"""Brain intent 回调 TLS 校验（P0 修复回归）。

背景（审计实锤）：backend :8000 为自签 HTTPS（CA=certs/ca.crt），
rtc_bridge/main.py 的 Brain 回调（POST {BRAIN_API_URL}/intent）此前用裸
urllib.request.urlopen，未构造 SSL 上下文 → 必然 CERTIFICATE_VERIFY_FAILED，
且异常被吞掉降级为 warning，AI 文本永远到不了 Brain。

修复契约（对齐 ack_reporter.py:64-66 / redemption.py:81-83 的既有模式）：
- ca 路径经环境变量解析（BRAIN_CA_FILE → SSL_CERT_FILE 回退，与
  RTC_BRIDGE_CONTROL_PLANE_CA_FILE 的注入方式一致）；
- 显式 ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=...)；
- ca 不可得时降级为显式 warning + 无 context 请求，不阻断语音会话
  （对齐 drain_ack.build_ack_reporter 的"能力不可用不阻断启动"策略）。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import ssl

import pytest

from rtc_bridge import main as brain_main


@pytest.fixture
def ca_file(tmp_path):
    p = tmp_path / "ca.crt"
    p.write_text("dummy-ca-body", encoding="utf-8")
    return str(p)


def _install_urlopen_spy(monkeypatch, captured):
    def fake_urlopen(req, timeout=0, context=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["timeout"] = timeout
        captured["context"] = context
        captured["data"] = req.data
        return type(
            "Resp",
            (),
            {
                "read": lambda self: json.dumps(
                    {"code": 0, "data": {"task_id": "t1"}}
                ).encode("utf-8"),
                "__enter__": lambda self: self,
                "__exit__": lambda self, *a: None,
            },
        )()

    monkeypatch.setattr(brain_main.urllib.request, "urlopen", fake_urlopen)


def test_brain_intent_uses_ssl_context_with_cafile(monkeypatch, ca_file):
    """修复核心断言：发请求必须用带 cafile 的 SSL context。"""
    captured: dict = {}
    _install_urlopen_spy(monkeypatch, captured)

    def fake_create_default_context(purpose=None, cafile=None):
        captured["purpose"] = purpose
        captured["cafile"] = cafile
        return object()  # 哑上下文；urlopen 已被替换，不会真正握手

    monkeypatch.setattr(
        ssl, "create_default_context", fake_create_default_context
    )
    monkeypatch.setenv("BRAIN_CA_FILE", ca_file)

    callback = brain_main._make_brain_callback(
        "https://127.0.0.1:8000/api/v1/brain"
    )
    asyncio.run(callback("帮我打开监控"))

    assert captured["cafile"] == ca_file, (
        "ssl.create_default_context 必须以 BRAIN_CA_FILE 指向的 ca 文件构造"
    )
    assert captured["purpose"] == ssl.Purpose.SERVER_AUTH
    assert captured["context"] is not None, (
        "urlopen 必须收到显式 SSL context（自签 HTTPS 需要 CA 校验）"
    )
    assert captured["url"] == "https://127.0.0.1:8000/api/v1/brain/intent"
    assert captured["method"] == "POST"


def test_brain_intent_degrades_without_ca_file(monkeypatch):
    """ca 不可得时降级不崩：仍发请求（既有"Brain 不可用降级"契约）。"""
    captured: dict = {}
    _install_urlopen_spy(monkeypatch, captured)
    monkeypatch.delenv("BRAIN_CA_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    callback = brain_main._make_brain_callback(
        "https://127.0.0.1:8000/api/v1/brain"
    )
    asyncio.run(callback("帮我打开监控"))

    assert "url" in captured, "ca 缺失时仍应发出请求（降级而非中断）"
    assert captured["url"].endswith("/intent")


def test_brain_ca_resolves_repo_default_when_env_path_missing(monkeypatch):
    """乱码/失效的 env 绝对路径必须回退到仓库相对 certs/ca.crt。

    现场实锤（2026-09-01 05:17 rtc_bridge.log.err）：.env 经 PS5.1
    Load-Env 注入时中文路径被 GBK 误解码（监视app → 鐩戣…），env 路径
    不存在 → TLS verification disabled (degraded)。契约：解析器必须做
    存在性校验，候选失效时回退 backend/../certs/ca.crt。
    """
    monkeypatch.setenv("BRAIN_CA_FILE", r"C:\nonexistent\鐩戣\certs\ca.crt")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    resolved = brain_main._resolve_brain_ca_file()
    expected = (
        pathlib.Path(brain_main.__file__).resolve().parents[2]
        / "certs"
        / "ca.crt"
    )
    assert resolved == str(expected), "env 路径失效时必须回退仓库相对 certs/ca.crt"
    assert pathlib.Path(resolved).is_file(), "回退路径必须真实存在"


def test_brain_ca_env_existing_path_takes_priority(monkeypatch, tmp_path):
    """存在且有效的 env 路径优先于仓库默认。"""
    p = tmp_path / "other-ca.crt"
    p.write_text("dummy", encoding="utf-8")
    monkeypatch.setenv("BRAIN_CA_FILE", str(p))
    assert brain_main._resolve_brain_ca_file() == str(p)
