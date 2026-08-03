"""主编排器：asyncio 监控循环 / 事件联动 / 语音会话调度

- 监控循环：按 monitors.yaml 轮询截屏 → 视觉分析 → 状态判定 → 事件
- 显存时分复用：对话期间自动降频（voice_active 标志）
- 单实例互斥：LLM 调用串行化（asyncio 锁）
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..capture.session_manager import SessionManager
from ..config import AppConfig
from ..core.events import EVT_SESSION_UPDATED, EventBus
from ..core.state import AgentState, SessionSnapshot, state
from ..engine.llama_omni_client import LlamaOmniClient
from ..engine.status_detector import DetectionResult, detect_status
from ..engine.vision_analyzer import VisionAnalyzer
from ..push.manager import PushManager
from ..services.reminder_service import ReminderService
from ..utils.metrics import metrics

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        cfg: AppConfig,
        bus: EventBus,
        client: LlamaOmniClient,
        analyzer: VisionAnalyzer,
        push: PushManager,
        reminder: ReminderService,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._client = client
        self._analyzer = analyzer
        self._push = push
        self._reminder = reminder
        self._sessions = SessionManager(
            cfg.monitors.monitors,
            Path(cfg.monitors.capture.tmp_dir),
            max_width=cfg.monitors.capture.max_width,
        )
        self._llm_lock = asyncio.Lock()
        self._voice_active = False
        self._monitor_enabled = True
        self._task: asyncio.Task | None = None
        self._last_alert_at: dict[str, float] = {}

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self._sessions.locate_all()
        for s in self._sessions.all():
            state.get_or_create(s.target.app_id, s.target.app_name)
            if s.target.enabled:
                self._sessions.start_wgc(s.target.app_id)
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("orchestrator started: %d targets", len(self._sessions.all()))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for s in self._sessions.all():
            if s.wgc:
                s.wgc.stop()

    def set_voice_active(self, active: bool) -> None:
        """语音对话期间调用：监控自动降频（显存时分复用）"""
        self._voice_active = active
        state.pet_state = "listening" if active else "monitoring"

    # ---------- 控制指令（公开 API，供 routes_control 调用） ----------
    def start_monitoring(self) -> None:
        self._monitor_enabled = True

    def stop_monitoring(self) -> None:
        self._monitor_enabled = False

    async def trigger_test_alert(self, app_id: str, level: int = 4) -> bool:
        """手动触发提醒测试（构造类型化 DetectionResult 走 _maybe_alert 内部逻辑）。

        替代旧 routes 里的 type("R", ...) 动态 hack；返回 False 表示 app_id 未知。
        """
        snap = state.get(app_id)
        if snap is None:
            return False
        result = DetectionResult(
            app_id=app_id,
            triggered=True,
            alert_level=level,
            reason="manual_test",
            state=snap.state,
            suggestion="测试提醒",
        )
        await self._maybe_alert(snap, result)
        return True

    # ---------- 监控循环 ----------
    async def _monitor_loop(self) -> None:
        while True:
            try:
                if self._monitor_enabled:
                    await self._tick_all()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("monitor loop tick failed")
                await asyncio.sleep(2.0)

    async def _tick_all(self) -> None:
        for session in self._sessions.all():
            target = session.target
            if not target.enabled:
                continue
            # 轮询间隔（对话期降频）
            interval = (
                self._cfg.monitors.voice_active_poll_interval_seconds
                if self._voice_active
                else target.poll_interval_seconds
            )
            snap = state.get(target.app_id)
            if snap is None:
                continue
            if time.time() - snap.last_frame_at < interval:
                continue
            await self._tick_one(target.app_id)

    async def _tick_one(self, app_id: str) -> None:
        snap = state.get(app_id)
        session = self._sessions.get(app_id)
        if snap is None or session is None:
            return

        frame_path = self._sessions.snapshot(app_id)
        if frame_path is None:
            self._update_unknown(snap, "捕获失败（窗口可能最小化）")
            return

        t0 = time.time()
        try:
            # 显存时分复用：LLM 调用串行化
            async with self._llm_lock:
                vision = await self._analyzer.analyze(frame_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("vision analyze failed: %s (%s)", app_id, e)
            self._update_unknown(snap, f"模型分析失败: {e}")
            return

        analysis_ms = int((time.time() - t0) * 1000)
        metrics.record_analysis(analysis_ms)
        snap.last_analysis_ms = analysis_ms
        snap.last_frame_at = time.time()
        snap.frame_count += 1
        snap.last_summary = vision.summary

        # 状态判定（含 3 帧 + 超时双条件）
        result = detect_status(snap, vision.state, self._cfg.detection)

        # 触发提醒
        if result.triggered:
            await self._maybe_alert(snap, result)

        # 广播会话状态
        await self._bus.emit(EVT_SESSION_UPDATED, snap.to_dict())

    def _update_unknown(self, snap: SessionSnapshot, reason: str) -> None:
        snap.state = AgentState.UNKNOWN
        snap.last_frame_at = time.time()

    async def _maybe_alert(self, snap: SessionSnapshot, result) -> None:
        # 提醒节流：同 app 最小间隔
        now = time.time()
        last = self._last_alert_at.get(snap.app_id, 0)
        if now - last < self._cfg.detection.min_alert_interval_seconds:
            return
        self._last_alert_at[snap.app_id] = now

        await self._reminder.on_detection(
            {
                "app_id": snap.app_id,
                "alert_level": result.alert_level,
                "state": result.state.value,
                "summary": snap.last_summary,
                "suggestion": result.suggestion,
            }
        )
