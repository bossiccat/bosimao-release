"""API 路由：voice 网关控制面 + WS /ws/voice（mobile-voice-spec §8.1/§10）

- WS   /ws/voice                 手机直连语音流（§7 协议；局域网直连同协议）
- WS   /api/v1/voice/stream      同处理器别名（spec §10 契约路径）
- GET  /api/v1/voice/status      语音网关状态（relay/phone/engine/path）
- POST /api/v1/voice/pair        配对码 + token 签发（M1 stub，V2 中继扩展）
- POST /api/v1/voice/session     TRTC 会话签发（ADR-012 / PC-INTEGRATION §2.3）

商业安全路由（Bearer/nonce/限流/fail-closed）见 routes_voice_secured 模块，
本模块只保留旧半双工网关与无状态签发装配（既有测试契约）。

只编排不写业务：握手/帧分发在 app.voice.session，处理链路在 app.voice.half_duplex，
TRTC 会话签发在 app.voice.rtc_session + app.voice.usersig。
"""
from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import load_settings
from ..voice.config import VoiceConfig, load_voice
from ..voice.half_duplex import HalfDuplex
from ..voice.rtc_session import (
    ConfigMissingError,
    InvalidDeviceIdError,
    InvalidUserIdError,
    RtcSessionService,
    build_session_service_from_settings,
)
from ..voice.session import VoiceSessionManager, handshake, run_session
from ..voice.stt_sherpa import SttSherpa
from ..voice.tts_edge import TtsEdge

# 商业安全路由工厂（SPEC §5 认证/nonce/限流/fail-closed；生产装配使用它）
from .routes_voice_secured import create_secured_voice_router  # noqa: F401

logger = logging.getLogger(__name__)


class VoiceSessionRequest(BaseModel):
    """POST /api/v1/voice/session 请求体（PC-INTEGRATION §2.3）"""

    device_id: str = Field(..., min_length=1, max_length=64, description="设备标识（幂等键）")
    pairing_code: str | None = Field(None, description="已废弃语义，MVP 可省略")


class VoiceSignRequest(BaseModel):
    """POST /api/v1/voice/session/sign 请求体（PC sidecar 取自身 userSig，PC-INTEGRATION §2.3）"""

    device_id: str = Field(..., min_length=1, max_length=64, description="设备标识（幂等键）")
    user_id: str = Field("jax-pc-sidecar", min_length=1, max_length=64, description="进房 userId（默认 PC sidecar）")


def build_voice_gateway(cfg: VoiceConfig | None = None) -> tuple[APIRouter, VoiceSessionManager]:
    """装配 voice 网关（入口调用；引擎依赖注入，测试可传自建 cfg/manager）

    path=apm：M3 云端全双工（MiniCPM-o Realtime API），无需本地 STT/TTS 引擎；
    其余路径：本地半双工（sherpa STT + brain/本地模型 + edge-tts）。
    """
    cfg = cfg or load_voice()
    if cfg.path == "apm":
        # 全双工引擎在会话级装配（ApmBridge），此处 manager 仅做会话注册
        manager = VoiceSessionManager(cfg, None)  # type: ignore[arg-type]
    else:
        stt = SttSherpa(cfg.half_duplex.stt_model_dir)
        tts = TtsEdge(voice=cfg.half_duplex.edge_tts_voice, cache_size=cfg.half_duplex.edge_tts_cache_size)
        engine = HalfDuplex(
            stt=stt,
            tts=tts,
            trigger_words=cfg.half_duplex.brain_trigger,
        )
        manager = VoiceSessionManager(cfg, engine)
    router = create_voice_router(cfg, manager)
    return router, manager


def create_voice_router(cfg: VoiceConfig, manager: VoiceSessionManager) -> APIRouter:
    # 注意：WS 端点固定在根路径 /ws/voice（任务 M1 契约），故不使用 prefix，
    # 控制面按 spec §10 挂 /api/v1/voice/* 全路径。
    router = APIRouter(tags=["voice"])

    @router.websocket("/ws/voice")
    async def ws_voice(ws: WebSocket) -> None:
        session = await handshake(ws, cfg, manager)
        if session is None:
            return
        await run_session(session, manager)

    @router.websocket("/api/v1/voice/stream")
    async def ws_voice_stream(ws: WebSocket) -> None:
        """局域网直连别名：/api/v1/voice/stream（与 /ws/voice 同协议同处理器）"""
        session = await handshake(ws, cfg, manager)
        if session is None:
            return
        await run_session(session, manager)

    @router.get("/api/v1/voice/status")
    async def voice_status() -> dict:
        stt_status = "missing"
        if manager.engine is not None:
            try:
                stt_status = manager.engine._stt.model_status()
            except Exception:  # noqa: BLE001
                stt_status = "error"
        data = manager.status()
        data["relay"] = "lan_direct"          # M1：局域网直连；中继客户端 relay_client 属 M2
        data["path"] = "A" if cfg.path == "apm" else "B"
        data["engine"] = "apm_realtime" if cfg.path == "apm" else "half_duplex"
        data["stt_model"] = stt_status
        data["tts"] = cfg.half_duplex.edge_tts_voice
        return {"code": 0, "data": data, "message": ""}

    @router.post("/api/v1/voice/pair")
    async def voice_pair() -> dict:
        """配对码 + token 签发（M1 stub：本地直连不强制，中继配对属 M2）"""
        code = f"{secrets.randbelow(1000000):06d}"
        token = cfg.token or secrets.token_urlsafe(16)
        return {"code": 0, "data": {
            "pairing_code": code, "token": token,
            "expires_in_s": 300, "ts": int(time.time()),
        }, "message": "配对码已签发"}

    return router


def build_session_router(service: RtcSessionService | None = None) -> APIRouter:
    """TRTC 会话签发路由（PC-INTEGRATION §2.3；独立装配，测试可注入 service）

    默认从 .env（Settings）装配；凭据缺失返回 503 + code=50300。
    """
    svc = service or build_session_service_from_settings(load_settings())
    router = APIRouter(tags=["voice"])

    @router.post("/api/v1/voice/session", status_code=201)
    async def voice_session(req: VoiceSessionRequest):
        """签发 TRTC 进房凭证：{device_id} → {room_id, user_id(=device_id), user_sig, sdk_app_id, scene}

        锁定契约（OpenAPI）：HTTP 201，scene=trtc_full_duplex，含 session_id/expires_at。
        生产装配使用 create_secured_voice_router（本函数保留供既有测试注入 service）。
        """
        try:
            data = svc.issue(req.device_id)
        except InvalidDeviceIdError as e:
            return JSONResponse(
                status_code=400, content={"code": 40001, "data": None, "message": str(e)}
            )
        except ConfigMissingError as e:
            return JSONResponse(
                status_code=503, content={"code": 50300, "data": None, "message": str(e)}
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice session issue failed device=%s", req.device_id)
            return JSONResponse(
                status_code=500,
                content={"code": 50000, "data": None, "message": "Internal server error"},
            )
        return {"code": 0, "data": data, "message": ""}

    @router.post("/api/v1/voice/session/sign", status_code=201)
    async def voice_session_sign(req: VoiceSignRequest):
        """PC sidecar 进房签发：{device_id, user_id} → 同一 room_id，userSig 签给 user_id

        本地 Phase B 测试辅助（生产路径为云函数 /sign，SecretKey 唯一存云函数环境变量；
        本地 .env 仅在冒烟/联调期持有，Phase B 生产路径 PC .env 置空）。
        """
        try:
            data = svc.sign(req.device_id, req.user_id)
        except (InvalidDeviceIdError, InvalidUserIdError) as e:
            return JSONResponse(
                status_code=400, content={"code": 40001, "data": None, "message": str(e)}
            )
        except ConfigMissingError as e:
            return JSONResponse(
                status_code=503, content={"code": 50300, "data": None, "message": str(e)}
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice session sign failed device=%s user=%s", req.device_id, req.user_id)
            return JSONResponse(
                status_code=500,
                content={"code": 50000, "data": None, "message": "Internal server error"},
            )
        return {"code": 0, "data": data, "message": ""}

    return router
