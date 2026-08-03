"""捕获会话生命周期管理 + 授权状态"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import MonitorTarget
from .dxgi_fallback import DxgiFallback
from .wgc_capture import WgcCapturer
from .window_finder import WindowInfo, find_window

logger = logging.getLogger(__name__)

# WGC 出过帧后超过该时长仍无新帧 → 判定窗口丢失/捕获停止
LOST_TIMEOUT_SECONDS = 30.0


@dataclass
class CaptureSession:
    target: MonitorTarget
    window: WindowInfo | None = None
    wgc: WgcCapturer | None = None
    dxgi: DxgiFallback | None = None
    authorized: bool = False
    last_frame_at: float = 0.0
    last_consumed_at: float = 0.0
    last_error: str = ""
    mode: str = "none"  # wgc / dxgi / lost / none

    def to_dict(self) -> dict:
        return {
            "app_id": self.target.app_id,
            "window_found": self.window is not None,
            "authorized": self.authorized,
            "mode": self.mode,
            "last_error": self.last_error,
        }


class SessionManager:
    """管理全部被监控窗口的捕获会话"""

    def __init__(
        self,
        targets: list[MonitorTarget],
        tmp_dir: Path,
        max_width: int = 1280,
    ) -> None:
        self._tmp_dir = tmp_dir
        self._max_width = max_width
        self._sessions: dict[str, CaptureSession] = {
            t.app_id: CaptureSession(target=t) for t in targets
        }

    def all(self) -> list[CaptureSession]:
        return list(self._sessions.values())

    def get(self, app_id: str) -> CaptureSession | None:
        return self._sessions.get(app_id)

    def locate_all(self) -> None:
        """重新定位所有窗口（窗口重启后可重新定位）"""
        for session in self._sessions.values():
            try:
                session.window = find_window(
                    session.target.process_name, session.target.window_title_regex
                )
                if session.window is None:
                    session.mode = "none"
                    session.last_error = "窗口未找到"
                else:
                    session.mode = "wgc" if session.authorized else "pending-auth"
            except Exception as e:  # noqa: BLE001
                session.last_error = str(e)
                logger.warning("locate failed: %s (%s)", session.target.app_id, e)

    def start_wgc(self, app_id: str) -> bool:
        """启动 WGC 捕获（需已授权；未授权需先走系统选择器）"""
        session = self._sessions.get(app_id)
        if session is None or session.window is None or not session.authorized:
            return False
        out_dir = self._tmp_dir / app_id
        session.wgc = WgcCapturer(
            window_title=session.window.title, output_dir=out_dir
        )
        session.wgc.start()
        session.mode = "wgc"
        return True

    def mark_authorized(self, app_id: str) -> None:
        session = self._sessions.get(app_id)
        if session:
            session.authorized = True

    def snapshot(self, app_id: str) -> Path | None:
        """取最近一帧截图（只消费新帧；无新帧降 DXGI 兜底）。

        返回 None 表示本次轮询无可用新帧（orchestrator 应置 UNKNOWN）：
        - WGC 已出过帧但 30s+ 无新帧 → 窗口丢失/捕获停止，mode="lost"
        - WGC 从未出帧或活跃期空帧 → 尝试 DXGI 兜底
        - DXGI 也失败 → 返回 None
        """
        session = self._sessions.get(app_id)
        if session is None:
            return None
        now = time.time()

        if session.wgc is not None:
            path, captured_at = session.wgc.snapshot()
            # 先判陈旧：WGC 已停/窗口丢失（即使有旧帧也不消费）
            if captured_at > 0 and now - captured_at >= LOST_TIMEOUT_SECONDS:
                session.mode = "lost"
                session.last_error = f"WGC 停止/窗口丢失超过 {int(LOST_TIMEOUT_SECONDS)}s"
                logger.warning("capture lost: app=%s (stale %.0fs)", app_id, now - captured_at)
                return None
            if captured_at > session.last_consumed_at:
                # 新帧：消费
                session.last_consumed_at = now
                session.mode = "wgc"
                return path
            # 活跃期空帧（或尚未出帧）：继续走 DXGI 兜底

        # DXGI 兜底：优先裁剪窗口区域，失败回退整屏
        if session.dxgi is None:
            session.dxgi = DxgiFallback(self._tmp_dir / f"dxgi-{app_id}", self._max_width)
        rect = session.window.rect if session.window is not None else None
        path = session.dxgi.capture_once(rect=rect)
        if path is not None:
            session.mode = "dxgi"
            session.last_consumed_at = now
            session.last_error = ""
            return path
        session.mode = "lost"
        session.last_error = "DXGI 兜底捕获失败"
        return None
