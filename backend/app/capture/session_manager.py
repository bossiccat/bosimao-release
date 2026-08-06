"""捕获会话生命周期管理 + 授权状态 + 最小化崩溃防御

契约：docs/specs/backend-capture-auth-spec.md（V1 立即实现部分）+ POC-002 保留项 3。
- 授权状态机：pending-auth → authorizing → authorized / denied → status-only（拒绝降级）
- 持久化：backend/data/authorized_windows.json（原子写，仅 app_id/window_title/authorized_at）
- 最小化防御：IsIconic 检测 → 主动停 WGC（避免 Trae/WorkBuddy 原生崩溃）→ DXGI 兜底 → 恢复重建
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import MonitorTarget
from .dxgi_fallback import DxgiFallback
from .wgc_capture import WgcCapturer, WgcTrialCapturer
from .window_finder import WindowInfo, find_window, is_window_minimized

logger = logging.getLogger(__name__)

# WGC 出过帧后超过该时长仍无新帧 → 判定窗口丢失/捕获停止
LOST_TIMEOUT_SECONDS = 30.0
# 授权超时：进入 authorizing 后 60s 无首帧/无异常 → 判定 denied（spec §7）
AUTH_TIMEOUT_SECONDS = 60.0
# WGC 崩溃后重建冷却：防止"崩溃→重建→再崩溃"每 tick 无限重建 → 线程堆积/事件循环冻结
REBUILD_COOLDOWN_SECONDS = 30.0

AUTH_FILE_VERSION = 1
# 授权持久化文件（backend/data/authorized_windows.json，仅存 app_id/window_title/authorized_at）
DEFAULT_AUTH_FILE = Path(__file__).resolve().parents[3] / "backend" / "data" / "authorized_windows.json"


def load_auth_file(path: Path) -> dict[str, dict]:
    """读取授权持久化；缺失/损坏 → {} 视为全部未授权（失败容忍，不阻断启动）。"""
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        out: dict[str, dict] = {}
        for w in doc.get("windows", []):
            if w.get("authorized") and w.get("app_id"):
                out[w["app_id"]] = w
        return out
    except Exception:  # noqa: BLE001
        logger.warning("authorized_windows.json 损坏，视为全部未授权: %s", path)
        return {}


def save_auth_file(path: Path, entries: list[dict]) -> bool:
    """原子写（临时文件 + os.replace）；失败仅记日志，不阻断授权流程。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = {"version": AUTH_FILE_VERSION, "windows": entries}
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("授权持久化失败: %s", path)
        return False


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
    # none / pending-auth / authorizing / authorized / denied / status-only / wgc / dxgi / lost
    mode: str = "none"
    minimized: bool = False           # 最近一次检测是否最小化（恢复时触发重建）
    last_rebuild_at: float = 0.0      # 最近一次 WGC 重建时间（崩溃重建冷却）

    @property
    def auth_status(self) -> str:
        """授权状态归一化（供 GET /api/v1/capture/status 消费）"""
        if self.authorized and self.mode in ("wgc", "dxgi"):
            return "authorized"
        if self.mode == "authorizing":
            return "authorizing"
        if self.mode in ("denied", "status-only"):
            return "denied"
        if self.mode == "lost":
            return "lost"
        if self.mode == "pending-auth":
            return "pending-auth"
        return "none"

    def to_dict(self) -> dict:
        return {
            "app_id": self.target.app_id,
            "app_name": self.target.app_name,
            "window_found": self.window is not None,
            "authorized": self.authorized,
            "mode": self.mode,
            "auth_status": self.auth_status,
            "last_error": self.last_error,
        }


class SessionManager:
    """管理全部被监控窗口的捕获会话"""

    def __init__(
        self,
        targets: list[MonitorTarget],
        tmp_dir: Path,
        max_width: int = 1280,
        auth_file: Path | None = None,
    ) -> None:
        self._tmp_dir = tmp_dir
        self._max_width = max_width
        self._auth_file = auth_file or DEFAULT_AUTH_FILE
        # 持久化命中 → 直接 authorized（重启免重授权）
        persisted = load_auth_file(self._auth_file)
        self._sessions: dict[str, CaptureSession] = {}
        for t in targets:
            session = CaptureSession(target=t)
            if t.app_id in persisted:
                session.authorized = True
            self._sessions[t.app_id] = session

    def all(self) -> list[CaptureSession]:
        return list(self._sessions.values())

    def get(self, app_id: str) -> CaptureSession | None:
        return self._sessions.get(app_id)

    # ---------- 窗口定位 ----------
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
                elif session.mode in ("denied", "status-only"):
                    # 拒绝降级态保持：本窗口本次运行不再自动重试（需显式 retry=true）
                    pass
                else:
                    session.mode = "wgc" if session.authorized else "pending-auth"
            except Exception as e:  # noqa: BLE001
                session.last_error = str(e)
                logger.warning("locate failed: %s (%s)", session.target.app_id, e)

    # ---------- WGC 生命周期 ----------
    def start_wgc(self, app_id: str) -> bool:
        """启动 WGC 捕获（需已授权；未授权需先走授权流程）。

        denied/status-only 窗口拒绝启动（spec §6：未重新授权前不启动 WGC）。
        """
        session = self._sessions.get(app_id)
        if session is None or session.window is None or not session.authorized:
            return False
        if session.mode in ("denied", "status-only"):
            session.last_error = "该窗口已拒绝授权，未重新授权前不启动 WGC"
            return False
        out_dir = self._tmp_dir / app_id
        session.wgc = WgcCapturer(
            window_title=session.window.title, output_dir=out_dir
        )
        session.wgc.start()
        session.mode = "wgc"
        return True

    def stop_wgc(self, app_id: str) -> bool:
        """停止 WGC 捕获并清理该会话帧文件（会话结束路径，ADR-010）"""
        session = self._sessions.get(app_id)
        if session is None or session.wgc is None:
            return False
        session.wgc.stop()
        session.wgc.cleanup()
        session.wgc = None
        session.mode = "none"
        return True

    def stop_all(self) -> None:
        """停止全部 WGC 会话并清理帧文件（orchestrator.stop 调用）"""
        for app_id in list(self._sessions):
            self.stop_wgc(app_id)

    # ---------- 授权状态机（backend-capture-auth-spec §2/§6） ----------
    def prepare_authorize(self, app_id: str, retry: bool = False) -> dict:
        """进入授权流程：前置校验 + 置 authorizing（幂等入口）。

        返回 {ok, code?, mode, app_id?, app_name?, hint?, error?}：
        - 40401 未知 app_id / 40402 窗口未找到
        - 40001 该窗口已授权 / 40901 授权进行中（重复触发）/ 40902 已拒绝需 retry=true
        """
        session = self._sessions.get(app_id)
        if session is None:
            return {"ok": False, "code": 40401, "error": f"未知 app_id: {app_id}"}
        if session.authorized:
            return {"ok": False, "code": 40001, "mode": session.mode, "error": "该窗口已授权"}
        if session.mode == "authorizing":
            return {"ok": False, "code": 40901, "mode": session.mode, "error": "授权进行中"}
        if session.mode in ("denied", "status-only") and not retry:
            return {
                "ok": False,
                "code": 40902,
                "mode": session.mode,
                "error": "该窗口已拒绝授权，需 retry=true 重试",
            }
        if session.window is None:
            return {"ok": False, "code": 40402, "mode": "none", "error": "窗口未找到，无法授权"}
        session.authorized = False
        session.last_error = ""
        session.mode = "authorizing"
        return {
            "ok": True,
            "code": 0,
            "mode": "authorizing",
            "app_id": app_id,
            "app_name": session.target.app_name,
            "hint": f"请在系统弹窗中允许捕获窗口 {session.window.title}",
        }

    def finish_authorize(self, app_id: str, timeout: float = AUTH_TIMEOUT_SECONDS) -> dict:
        """执行试捕获（首次自动弹系统选择器）并回写结果。

        返回 {ok, mode, authorized, error?}；成功 mode=wgc 且落盘，失败降级 status-only。
        """
        session = self._sessions.get(app_id)
        if session is None:
            return {"ok": False, "mode": "none", "authorized": False, "error": "未知 app_id"}
        if session.window is None:
            session.mode = "none"
            return {"ok": False, "mode": "none", "authorized": False, "error": "窗口丢失"}
        ok, err = self._trial_capture(session, timeout)
        if ok:
            self.mark_authorized(app_id)
            session.mode = "wgc"
            session.last_error = ""
            return {"ok": True, "mode": "wgc", "authorized": True, "error": None}
        self.mark_denied(app_id, err or "授权失败")
        return {"ok": False, "mode": "status-only", "authorized": False, "error": session.last_error}

    def _trial_capture(self, session: CaptureSession, timeout: float) -> tuple[bool, str]:
        """试捕获：启动一次性 WGC（首次自动弹系统选择器），首帧即停。

        返回 (ok, error)；成功=出首帧，失败=超时/选择器不可用/线程异常。
        独立线程 + join(timeout) 保护：即使系统选择器阻塞也不挂死调用方。
        """
        trial_dir = self._tmp_dir / f"auth-{session.target.app_id}"
        try:
            capturer = WgcTrialCapturer(
                window_title=session.window.title, output_dir=trial_dir
            )
        except Exception as e:  # noqa: BLE001
            return False, f"系统选择器不可用: {e}"
        thread = threading.Thread(
            target=capturer.run, daemon=True, name=f"auth-{session.target.app_id}"
        )
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            capturer.request_stop()  # 尽力停止（下一帧到达时关闭）
            return False, "授权超时"
        return capturer.result

    def mark_authorized(self, app_id: str) -> None:
        """授权成功：置 authorized 并原子落盘（不含 token/webhook/截图/命令行）"""
        session = self._sessions.get(app_id)
        if session is None:
            return
        session.authorized = True
        self._persist_authorized()

    def mark_denied(self, app_id: str, reason: str) -> None:
        """授权拒绝/超时：降级 status-only（仅窗口存在性/进程状态监控，不截屏分析）"""
        session = self._sessions.get(app_id)
        if session is None:
            return
        session.authorized = False
        session.mode = "status-only"
        session.last_error = f"授权被拒: {reason}"

    def _persist_authorized(self) -> None:
        """重建 authorized_windows.json（原子写）"""
        entries: list[dict] = []
        for session in self._sessions.values():
            if not session.authorized:
                continue
            entries.append(
                {
                    "app_id": session.target.app_id,
                    "window_title": session.window.title if session.window else "",
                    "authorized": True,
                    "authorized_at": int(time.time()),
                }
            )
        save_auth_file(self._auth_file, entries)

    # ---------- 最小化/崩溃防御（POC-002 保留项 3） ----------
    def is_minimized(self, app_id: str) -> bool:
        """窗口是否最小化（IsIconic）"""
        session = self._sessions.get(app_id)
        if session is None or session.window is None:
            return False
        return is_window_minimized(session.window.hwnd)

    def wgc_alive(self, app_id: str) -> bool:
        """WGC 会话是否存活（on_closed/崩溃后 is_running=False）"""
        session = self._sessions.get(app_id)
        if session is None or session.wgc is None:
            return False
        return session.wgc.is_running()

    def handle_minimized(self, app_id: str) -> bool:
        """窗口最小化/WGC 崩溃 → 主动停 WGC（避免原生崩溃），切 DXGI 兜底。

        返回 True 表示本轮有 WGC 会话被停（幂等：已停则 False）。
        """
        session = self._sessions.get(app_id)
        if session is None or session.wgc is None:
            return False
        try:
            session.wgc.stop()
            session.wgc.cleanup()
        except Exception:  # noqa: BLE001
            logger.warning("minimized cleanup failed: %s", app_id)
        session.wgc = None
        session.last_error = "窗口最小化，WGC 已停（DXGI 兜底）"
        session.mode = "dxgi"
        return True

    def handle_restored(self, app_id: str) -> bool:
        """窗口恢复可见 → 重建 WGC 会话（重新定位 + start_wgc）。返回 True 表示已重建。"""
        session = self._sessions.get(app_id)
        if session is None or not session.authorized:
            return False
        if session.mode in ("denied", "status-only"):
            return False
        try:
            session.window = find_window(
                session.target.process_name, session.target.window_title_regex
            )
        except Exception as e:  # noqa: BLE001
            session.last_error = str(e)
            return False
        if session.window is None:
            session.mode = "none"
            session.last_error = "窗口未找到"
            return False
        return self.start_wgc(app_id)

    def rebuild_due(self, app_id: str, now: float | None = None) -> bool:
        """WGC 崩溃/缺失后是否允许重建（冷却期内不重建）。

        背景：WGC 原生崩溃→重建→再崩溃会形成每 tick 无限重建，线程堆积 → 事件循环冻结。
        冷却期内保持 DXGI 兜底；恢复/最小化转换可即时重建（不受此限）。
        """
        session = self._sessions.get(app_id)
        if session is None:
            return False
        now = now or time.time()
        return now - session.last_rebuild_at >= REBUILD_COOLDOWN_SECONDS

    # ---------- 快照 ----------
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
