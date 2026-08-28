"""Secured voice status and legacy device WebSocket stream routes."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..voice.auth import AuthError, CredentialPrincipal

WS_CLOSE_AUTH = 4401
WS_CLOSE_NONCE = 4402
WS_CLOSE_BINDING = 4403
WS_CLOSE_GATE = 4503
WS_CLOSE_HELLO_TIMEOUT = 4408


def build_voice_stream_router(deps: Any) -> APIRouter:
    router = APIRouter(tags=["voice"])

    @router.get("/api/v1/voice/status")
    async def voice_status(request: Request):
        if deps.runtime_missing():
            return deps.error(50300)
        token = deps.resolve_bearer(request.headers.get("authorization", ""))
        if token is None:
            return deps.error(40101)
        principal = resolve_status_principal(deps, token)
        if principal is None:
            return deps.error(40101)
        allowed, _retry = deps.limiter.check(
            principal.subject_id, _client_ip(request), "voice:status"
        )
        if not allowed:
            return deps.error(42901)
        events = deps.store.list_session_events(principal.subject_id, limit=20)
        latest = events[0] if events else None
        data = {
            "session_id": latest["session_id"] if latest else None,
            "turn_id": None,
            "state": latest["state"] if latest and latest["state"] else "IDLE",
            "up_frame_count": 0,
            "up_bytes": 0,
            "down_frame_count": 0,
            "down_bytes": 0,
            "first_remote_audio_ts": None,
            "first_nonzero_playback_ts": None,
            "queue_depth": 0,
            "queue_high_watermark": 0,
            "queue_drops": 0,
            "backpressure_events": 0,
            "reconnects": 0,
            "error_code": None,
        }
        return {"code": 0, "data": data, "message": ""}

    @router.websocket("/api/v1/voice/stream")
    async def voice_stream(ws: WebSocket) -> None:
        if deps.runtime_missing():
            await ws.close(code=WS_CLOSE_GATE)
            return
        token = deps.resolve_bearer(ws.headers.get("authorization", ""))
        if token is None:
            await ws.close(code=WS_CLOSE_AUTH)
            return
        try:
            principal = deps.validator.verify_device(token)
        except AuthError:
            await ws.close(code=WS_CLOSE_AUTH)
            return
        nonce = ws.headers.get("x-request-nonce", "")
        if not deps.nonces.consume(principal, nonce):
            await ws.close(code=WS_CLOSE_NONCE)
            return
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except Exception:  # noqa: BLE001
            await ws.close(code=WS_CLOSE_HELLO_TIMEOUT)
            return
        if (not isinstance(hello, dict) or hello.get("type") != "hello"
                or hello.get("device_id") != principal.subject_id):
            await ws.close(code=WS_CLOSE_BINDING)
            return
        await ws.send_json({
            "type": "ready", "session_id": hello.get("session_id"),
            "device_id": principal.subject_id,
        })
        _record_session_event(
            deps, str(hello.get("session_id")), principal.subject_id,
            "stream_ready", "IN_ROOM",
        )
        try:
            while True:
                frame = await ws.receive_json()
                device = deps.store.get_device(principal.subject_id)
                if (device is None or device.status == "revoked"
                        or device.revoked_at is not None):
                    await ws.close(code=WS_CLOSE_AUTH, reason="credential_revoked")
                    break
                if isinstance(frame, dict) and frame.get("type") == "close":
                    break
        except WebSocketDisconnect:
            return

    return router


def resolve_status_principal(deps: Any, token: str) -> CredentialPrincipal | None:
    try:
        return deps.validator.verify_device(token)
    except AuthError:
        return _resolve_sidecar_principal(deps, token)


def _resolve_sidecar_principal(deps: Any, token: str) -> CredentialPrincipal | None:
    try:
        return deps.validator.verify_sidecar(token)
    except AuthError:
        return None


def _record_session_event(deps: Any, session_id: str, device_id: str,
                          event_type: str, state: str) -> None:
    try:
        deps.store.write_session_event(
            session_id=session_id, device_id=device_id, event_type=event_type, state=state
        )
    except Exception:  # noqa: BLE001
        return


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
