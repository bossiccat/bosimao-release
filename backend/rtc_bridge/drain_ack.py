"""rtc_bridge 终止上下文与 drain 上报支撑。

自 #22 起从 server.py / ack_reporter.py 外移，保持各文件 <300 行：
- TerminationRegistry：session_id → termination_id 注册表（唯一注入来源）
- parse_note_termination：sidecar 上行 ctrl「note_termination」载荷校验
- build_ack_reporter：凭据装配（缺配置返回 None = 禁用上报，不阻断桥启动）
- DrainAcknowledger：单连接 fire-once drain 上报状态机（失败只记日志、绝不冒泡）

中继链路：terminate 调用方获知 tid → sidecar 经既有 bridge WS 上行 ctrl
note_termination → rtc_bridge 校验活动会话匹配后注入注册表 → drain 时上报。
无任何注入 → drain 跳过上报（不硬编码、不伪造）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

MAX_TERMINATION_ID_LEN = 128
MAX_SESSION_ID_LEN = 64


class TerminationRegistry:
    """session_id → termination_id；由控制面中继经 WS ctrl 或进程内接缝注入。"""

    def __init__(self) -> None:
        self._by_session: dict[str, str] = {}

    def note(self, session_id: str, termination_id: str) -> bool:
        if not session_id or not termination_id:
            return False
        if len(session_id) > MAX_SESSION_ID_LEN:
            return False
        if len(termination_id) > MAX_TERMINATION_ID_LEN:
            return False
        self._by_session[session_id] = termination_id
        return True

    def get(self, session_id: str) -> str | None:
        return self._by_session.get(session_id)


def parse_note_termination(msg: Any) -> tuple[str, str] | None:
    """校验上行 {type:'ctrl', action:'note_termination'} 载荷。

    合法返回 (session_id, termination_id)；畸形/非本动作返回 None（调用方只记日志）。
    """
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "ctrl" or msg.get("action") != "note_termination":
        return None
    session_id = msg.get("session_id")
    termination_id = msg.get("termination_id")
    if not isinstance(session_id, str) or not isinstance(termination_id, str):
        return None
    if not session_id or not termination_id:
        return None
    if len(session_id) > MAX_SESSION_ID_LEN:
        return None
    if len(termination_id) > MAX_TERMINATION_ID_LEN:
        return None
    return session_id, termination_id


def build_ack_reporter(cfg, injected=None):
    """注入优先；否则凭据齐全时构建；缺配置/构建失败 → None（drain 全部跳过）。"""
    if injected is not None:
        return injected
    from .ack_reporter import AckReporterClient

    fields = (
        "control_plane_base_url", "control_plane_service_credential",
        "control_plane_ca_file", "control_plane_client_cert_file",
        "control_plane_client_key_file", "control_plane_gateway_assertion",
    )
    if not all(getattr(cfg, name, "") for name in fields):
        return None
    try:
        return AckReporterClient(
            base_url=cfg.control_plane_base_url,
            service_credential=cfg.control_plane_service_credential,
            ca_file=cfg.control_plane_ca_file,
            client_cert_file=cfg.control_plane_client_cert_file,
            client_key_file=cfg.control_plane_client_key_file,
            gateway_assertion=cfg.control_plane_gateway_assertion,
            connect_timeout_s=cfg.control_plane_connect_timeout_s,
            total_timeout_s=cfg.control_plane_total_timeout_s,
        )
    except Exception:  # noqa: BLE001 - 上报能力不可用不阻断桥启动
        logger.debug("ack reporter unavailable; drain reports disabled",
                     exc_info=True)
        return None


class DrainAcknowledger:
    """一次 sidecar 连接会话的 drain 上报（fire-once、fail-safe）。"""

    def __init__(
        self, *, session_id: str, device_id: str, room_id: str, generation: int,
        reporter: object | None, terminations: Mapping[str, str],
    ) -> None:
        self._session_id = session_id
        self._device_id = device_id
        self._room_id = room_id
        self._generation = generation
        self._reporter = reporter
        self._terminations = terminations
        self._reported = False
        self._apm_reported = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def bind(self, termination_id: str) -> None:
        """本连接内直接绑定终止上下文（优先于共享注册表查找）。"""
        self._terminations = {
            **self._terminations, self._session_id: termination_id,
        }

    async def report_drain(self, *, closed_cleanly: bool = True,
                           error_code: str | None = None) -> None:
        """drain 完成后调用；无终止上下文则跳过；任何失败只记日志。"""
        if self._reported or self._reporter is None:
            return
        termination_id = self._terminations.get(self._session_id)
        if not termination_id:
            logger.debug(
                "drain ack skipped: no termination context session=%s",
                self._session_id,
            )
            return
        self._reported = True
        result = "confirmed" if closed_cleanly else "failed"
        report_error_code = None if closed_cleanly else (
            error_code or "bridge_session_close_failed"
        )
        try:
            await self._reporter.report_drained_closed(
                termination_id=termination_id,
                session_id=self._session_id,
                device_id=self._device_id,
                room_id=self._room_id,
                generation=self._generation,
                result=result,
                error_code=report_error_code,
            )
        except Exception:  # noqa: BLE001 - 上报失败绝不能影响语音主链路
            logger.warning(
                "bridge drain ack report failed session=%s tid=%s",
                self._session_id, termination_id, exc_info=True,
            )

    async def report_apm_cancel(self, *, closed_cleanly: bool = True) -> None:
        """APM 会话取消关闭后调用（fire-once、fail-safe，语义同 report_drain）。

        仅当 APM 曾真实激活且存在终止上下文时上报 apm_cancelled_closed；
        无上下文跳过；任何失败只记日志。
        """
        if self._apm_reported or self._reporter is None:
            return
        termination_id = self._terminations.get(self._session_id)
        if not termination_id:
            logger.debug(
                "apm cancel ack skipped: no termination context session=%s",
                self._session_id,
            )
            return
        self._apm_reported = True
        result = "confirmed" if closed_cleanly else "failed"
        error_code = None if closed_cleanly else "apm_close_failed"
        try:
            await self._reporter.report_apm_cancelled_closed(
                termination_id=termination_id,
                session_id=self._session_id,
                device_id=self._device_id,
                room_id=self._room_id,
                generation=self._generation,
                result=result,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - 上报失败绝不能影响语音主链路
            logger.warning(
                "bridge apm cancel ack report failed session=%s tid=%s",
                self._session_id, termination_id, exc_info=True,
            )
