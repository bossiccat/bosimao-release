"""CloudRun 入口的端到端可用性契约（真发 HTTP 请求，真走序列化）。

背景（两个真实事故，本地 1000+ 单测全绿却都在容器里炸）：

1. `@asynccontextmanager` 没 import → 容器启动 NameError（部署 005）。
2. `/health` 与 `/api/v1/voice/cloud/status` 把 `service.is_configured`（**方法**，
   不是 property）直接放进响应体 → FastAPI 序列化失败，两个端点全 500（部署 006）。

根因是同一个：**没有任何测试真正加载并请求过 `cloudapi/main.py`**——它只在容器里
被 uvicorn import，所以这两个错误只有部署时才暴露。本文件把它当普通 ASGI app 起来，
用 TestClient 真发请求，把"能不能起来 + 能不能序列化"钉进 CI。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"

# 自检端点需要 owner 凭证；开发模式用固定测试值（生产由 CloudRun 环境变量注入）。
TEST_OWNER_CREDENTIAL = "selfcheck-contract-owner-credential"


def _load_entrypoint(monkeypatch: pytest.MonkeyPatch):
    """以开发模式加载 cloudapi/main.py，返回模块。

    开发模式（不设 VOICE_PRODUCTION）走 SQLite 夹具，无需 psycopg 与真实数据库，
    正是我们想在这里验证的"入口自身能否工作"。
    """
    monkeypatch.delenv("VOICE_PRODUCTION", raising=False)
    monkeypatch.setenv("VOICE_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("VOICE_OWNER_CREDENTIAL", TEST_OWNER_CREDENTIAL)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    spec = importlib.util.spec_from_file_location(
        "jax_voice_api_entrypoint", ROOT / "cloudapi" / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    module = _load_entrypoint(monkeypatch)
    return TestClient(module.app)


def test_health_returns_serialisable_json(client: TestClient) -> None:
    """`/health` 必须 200 且返回 JSON。

    这里曾经 500：响应体里放了绑定方法（`service.is_configured` 未加括号），
    FastAPI 抛 PydanticSerializationError: Unable to serialize unknown type: <class 'method'>。
    """
    response = client.get("/health")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "jax-voice-api"
    assert isinstance(payload["trtc_configured"], bool)
    assert isinstance(payload["security_ready"], bool)


def test_cloud_status_returns_serialisable_json(client: TestClient) -> None:
    """`/api/v1/voice/cloud/status` 同样必须可序列化（部署核验端点）。"""
    response = client.get("/api/v1/voice/cloud/status")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["service"] == "jax-voice-api"
    assert isinstance(payload["data"]["trtc_configured"], bool)
    assert isinstance(payload["data"]["security_missing"], list)
    # 存储探针：进程活着 ≠ 存储可用。线上曾出现部署成功、/health 200，
    # 但任何用到存储的端点都 500 —— 这条字段让那种情况一眼可见。
    assert isinstance(payload["data"]["storage"], str)


def test_selfcheck_walks_the_control_plane_write_path(client: TestClient) -> None:
    """自检端点必须由服务自身跑通全部写入步骤。

    这是把"验证"从外部脚本搬进产品：服务自己对真实存储执行
    nonce → 限流 → 配对码签发/消费 → 审计，逐步报告结果。
    """
    response = client.post(
        "/api/v1/voice/cloud/selfcheck",
        headers={"Authorization": f"Bearer {TEST_OWNER_CREDENTIAL}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["ok"] is True, payload["data"]["steps"]
    assert payload["data"]["steps"] == {
        "nonce": "ok",
        "rate_limit": "ok",
        "pairing_create": "ok",
        "pairing_consume": "ok",
        "audit": "ok",
    }


def test_selfcheck_requires_owner_credential(client: TestClient) -> None:
    """不能匿名触发写入自检。"""
    assert client.post("/api/v1/voice/cloud/selfcheck").status_code == 401
    assert client.post(
        "/api/v1/voice/cloud/selfcheck", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
