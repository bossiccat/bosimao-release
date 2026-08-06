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
from ..core.events import (
    EVT_AUTH_PROMPT,
    EVT_AUTH_RESULT,
    EVT_SESSION_UPDATED,
    EventBus,
)
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
                # 已授权窗口自动启动 WGC（原生捕获移出事件循环，防 GIL/COM 阻塞）
                await asyncio.to_thread(self._sessions.start_wgc, s.target.app_id)
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("orchestrator started: %d targets", len(self._sessions.all()))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        # ADR-010：会话停止即清理该会话全部帧文件（防 tmp/captures 无限堆积）
        self._sessions.stop_all()

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

    # ---------- WGC 授权流程（backend-capture-auth-spec §3/§4） ----------
    async def authorize_capture(self, app_id: str, retry: bool = False) -> dict:
        """授权入口：进入 authorizing → WS auth_prompt → 试捕获 → 回写 → WS auth_result → start_wgc。

        试捕获在后台线程执行（避免阻塞事件循环）；返回 SessionManager 判定结果。
        """
        res = self._sessions.prepare_authorize(app_id, retry)
        if not res["ok"]:
            return res
        await self._bus.emit(
            EVT_AUTH_PROMPT,
            {
                "app_id": app_id,
                "app_name": res.get("app_name", app_id),
                "hint": res.get("hint", ""),
            },
        )
        trial = await asyncio.to_thread(self._sessions.finish_authorize, app_id)
        await self._bus.emit(
            EVT_AUTH_RESULT,
            {
                "app_id": app_id,
                "ok": trial["ok"],
                "mode": trial.get("mode", "status-only"),
                "error": trial.get("error"),
            },
        )
        if trial["ok"]:
            # 授权成功 → 真正启动 WGC 会话（原生捕获移出事件循环，防 GIL/COM 阻塞）
            await asyncio.to_thread(self._sessions.start_wgc, app_id)
        return trial

    def capture_status(self) -> list[dict]:
        """各窗口授权状态（GET /api/v1/capture/status 消费）"""
        return [s.to_dict() for s in self._sessions.all()]

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
            snap = state.get(target.app_id)
            if snap is None:
                continue
            # 捕获模式同步：pending-auth/authorizing/wgc/dxgi/status-only/lost → UI
            # （spec §4：授权过程中 capture_mode 变化随监控循环广播）
            snap.capture_mode = session.mode
            snap.window_found = session.window is not None
            # 轮询间隔（对话期降频）
            interval = (
                self._cfg.monitors.voice_active_poll_interval_seconds
                if self._voice_active
                else target.poll_interval_seconds
            )
            if time.time() - snap.last_frame_at < interval:
                continue
            await self._tick_one(target.app_id)

    async def _tick_one(self, app_id: str) -> None:
        snap = state.get(app_id)
        session = self._sessions.get(app_id)
        if snap is None or session is None:
            return

        # 拒绝降级（status-only）：仅窗口存在性/进程状态监控，不截屏分析（spec §7）
        if session.mode == "status-only":
            snap.state = AgentState.UNKNOWN
            snap.capture_mode = "status-only"
            snap.window_found = session.window is not None
            snap.last_summary = "未授权，仅状态监控"
            await self._bus.emit(EVT_SESSION_UPDATED, snap.to_dict())
            return

        # 最小化/崩溃防御（POC-002 保留项 3：Trae 最小化必崩 WGC）
        minimized = self._sessions.is_minimized(app_id)

        if minimized:
            # 最小化：主动停 WGC（避免原生崩溃）→ DXGI 兜底
            if self._sessions.handle_minimized(app_id):
                logger.info("minimized: stopped wgc app=%s (dxgi fallback)", app_id)
            session.minimized = True
            snap.capture_mode = "dxgi"
            frame_path = self._sessions.snapshot(app_id)
            if frame_path is None:
                self._update_unknown(snap, "窗口最小化，仅状态监控")
                snap.window_found = session.window is not None
                await self._bus.emit(EVT_SESSION_UPDATED, snap.to_dict())
                return
        else:
            # 窗口可见：恢复/重建 WGC
            if session.minimized:
                # 刚从最小化恢复 → 即时重建（合法恢复路径，不受冷却限制）
                session.minimized = False
                if session.authorized and self._sessions.handle_restored(app_id):
                    session.last_rebuild_at = time.time()
                    logger.info("wgc rebuilt after restore: app=%s", app_id)
            elif session.wgc is None and session.authorized:
                # WGC 缺失（未启动/已停）→ 节流重建（防崩溃循环线程堆积）
                if self._sessions.rebuild_due(app_id):
                    if self._sessions.handle_restored(app_id):
                        session.last_rebuild_at = time.time()
                        logger.info("wgc rebuilt: app=%s", app_id)
            elif session.wgc is not None and not session.wgc.is_running():
                # WGC 崩溃（on_closed/进程 exit）：清理崩溃会话 → 节流重建
                self._sessions.handle_minimized(app_id)
                if self._sessions.rebuild_due(app_id):
                    if self._sessions.handle_restored(app_id):
                        session.last_rebuild_at = time.time()
                        logger.info("wgc rebuilt after crash: app=%s", app_id)
            frame_path = self._sessions.snapshot(app_id)
            if frame_path is None:
                self._update_unknown(snap, "捕获失败（窗口可能最小化）")
                snap.window_found = session.window is not None
                await self._bus.emit(EVT_SESSION_UPDATED, snap.to_dict())
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
