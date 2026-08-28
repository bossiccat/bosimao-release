from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes_agent_threads import create_agent_thread_router
from app.brain.agent_thread_registry import AgentThreadRegistry
from app.voice.auth import CredentialValidator
from app.voice.nonce import NonceService
from app.voice.rate_limit import RateLimitConfig, RateLimiter
from app.voice.storage import VoiceStore


OWNER_SECRET = "owner-secret"


def _nonce() -> str:
    return uuid.uuid4().hex


def _fixture(tmp_path: Path) -> tuple[AgentThreadRegistry, TestClient]:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    validator = CredentialValidator(
        store, CredentialValidator.hash_credential(OWNER_SECRET)
    )
    app = FastAPI()
    app.include_router(
        create_agent_thread_router(
            registry=registry,
            validator=validator,
            nonces=NonceService(store),
            limiter=RateLimiter(
                store, RateLimitConfig(window_seconds=60, device_limit=100, ip_limit=100)
            ),
        )
    )
    return registry, TestClient(app)


def _pending_approval(registry: AgentThreadRegistry) -> dict:
    thread = registry.handle_tool("spawn_agent_thread", {"user_speech": "safe probe"})
    assert thread["status"] == "awaiting_approval"
    return thread


def _headers(nonce: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {OWNER_SECRET}"}
    if nonce is not None:
        headers["X-Request-Nonce"] = nonce
    return headers


def test_approval_route_requires_owner_bearer_and_fresh_nonce(tmp_path: Path) -> None:
    registry, client = _fixture(tmp_path)
    thread = _pending_approval(registry)
    body = {"approval_id": thread["approval_id"]}

    no_auth = client.post(f"/api/v1/brain/threads/{thread['thread_id']}/approve", json=body)
    assert no_auth.status_code == 401
    assert no_auth.json()["code"] == 40101

    no_nonce = client.post(
        f"/api/v1/brain/threads/{thread['thread_id']}/approve",
        json=body,
        headers=_headers(),
    )
    assert no_nonce.status_code == 401
    assert no_nonce.json()["code"] == 40102


def test_approval_route_consumes_nonce_and_enqueues_one_worker_command(tmp_path: Path) -> None:
    registry, client = _fixture(tmp_path)
    thread = _pending_approval(registry)
    nonce = _nonce()
    body = {"approval_id": thread["approval_id"]}
    path = f"/api/v1/brain/threads/{thread['thread_id']}/approve"

    approved = client.post(path, json=body, headers=_headers(nonce))
    assert approved.status_code == 200
    assert approved.json()["code"] == 0
    assert registry.claim_commands() != []

    replayed = client.post(path, json=body, headers=_headers(nonce))
    assert replayed.status_code == 401
    assert replayed.json()["code"] == 40102


def test_approval_route_rejects_wrong_approval_id_with_conflict(tmp_path: Path) -> None:
    registry, client = _fixture(tmp_path)
    thread = _pending_approval(registry)

    response = client.post(
        f"/api/v1/brain/threads/{thread['thread_id']}/approve",
        json={"approval_id": "x" * 32},
        headers=_headers(_nonce()),
    )

    assert response.status_code == 409
    assert response.json()["code"] == 40901
