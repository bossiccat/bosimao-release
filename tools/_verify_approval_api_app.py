"""验证用最小 FastAPI app：只挂审批路由（独立 owner 凭据，与生产隔离）"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from fastapi import FastAPI  # noqa: E402

from app.api.routes_agent_threads import create_agent_thread_router  # noqa: E402
from app.brain.agent_thread_registry import AgentThreadRegistry  # noqa: E402
from app.voice.auth import CredentialValidator  # noqa: E402
from app.voice.nonce import NonceService  # noqa: E402
from app.voice.rate_limit import RateLimitConfig, RateLimiter  # noqa: E402
from app.voice.storage import VoiceStore  # noqa: E402

OWNER_SECRET = "verify-owner-e2e-secret-0827"

app = FastAPI()
store = VoiceStore(Path(os.environ["VOICE_DB_PATH"]))
store.initialize()
registry = AgentThreadRegistry(os.environ["AGENT_THREAD_DB"])
validator = CredentialValidator(store, CredentialValidator.hash_credential(OWNER_SECRET))


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


app.include_router(create_agent_thread_router(
    registry=registry,
    validator=validator,
    nonces=NonceService(store),
    limiter=RateLimiter(store, RateLimitConfig(window_seconds=60, device_limit=100, ip_limit=100)),
))
