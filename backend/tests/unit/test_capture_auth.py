"""WGC 授权流程单元测试（backend-capture-auth-spec V1 部分，全部 mock）

覆盖契约（spec §9 验收清单）：
- 状态流转：pending-auth → authorizing → authorized → wgc；拒绝 → status-only
- 持久化原子写：authorized_windows.json 仅含 app_id/window_title/authorized_at，无敏感信息
- 重启加载已授权窗口 → 直接 wgc（免重授权）
- 拒绝降级：status-only 后 start_wgc 拒绝、orchestrator 不再截屏分析
- prepare_authorize 错误码：40401/40001/40901/40902/40402
- WS 事件：auth_prompt/auth_result 信封 + orchestrator 成功时 start_wgc
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import app.capture.session_manager as sm_module
from app.capture.session_manager import CaptureSession, SessionManager
from app.capture.window_finder import WindowInfo
from app.config import AppConfig, MonitorTarget
from app.core.events import EVT_AUTH_PROMPT, EVT_AUTH_RESULT, EventBus
from app.core.orchestrator import Orchestrator
from app.core.state import AgentState


class FakeWgc:
    """mock WgcCapturer：start 时立即出帧（模拟首次捕获自动授权出帧）"""

    instances: list["FakeWgc"] = []

    def __init__(
        self, window_title: str = "", output_dir: Path | None = None, on_frame=None
    ) -> None:
        self.window_title = window_title
        self.output_dir = output_dir
        self.started = False
        self.running = False
        self.frame_ts = 0.0
        FakeWgc.instances.append(self)

    def start(self) -> None:
        self.started = True
        self.running = True
        self.frame_ts = time.time()  # 模拟首帧到达

    def stop(self) -> None:
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def snapshot(self) -> tuple[Path | None, float]:
        return None, self.frame_ts

    def cleanup(self) -> None:
        pass


class FakeDxgi:
    def __init__(self, output_dir: Path, max_width: int = 1280) -> None:
        self.capture_calls = 0

    def capture_once(self, rect=None) -> Path | None:
        self.capture_calls += 1
        return Path("dxgi.png")


class FakeTrial:
    """mock WgcTrialCapturer：试捕获立即成功（首帧即停）"""

    def __init__(self, window_title: str = "", output_dir: Path | None = None) -> None:
        self._ok, self._err = True, ""

    def run(self) -> None:
        pass

    def request_stop(self) -> None:
        pass

    @property
    def result(self):
        return self._ok, self._err


def make_window(title="Codex") -> WindowInfo:
    return WindowInfo(hwnd=1, title=title, pid=10, process_name="codex.exe")


@pytest.fixture(autouse=True)
def _clean_fakes():
    FakeWgc.instances.clear()
    yield
    FakeWgc.instances.clear()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    targets = [
        MonitorTarget(app_id="codex", app_name="Codex", process_name="codex.exe"),
        MonitorTarget(app_id="trae", app_name="Trae", process_name="trae.exe"),
    ]
    monkeypatch.setattr(sm_module, "WgcCapturer", FakeWgc)
    monkeypatch.setattr(sm_module, "DxgiFallback", FakeDxgi)
    monkeypatch.setattr(sm_module, "WgcTrialCapturer", FakeTrial)
    return SessionManager(
        targets, tmp_path, auth_file=tmp_path / "authorized_windows.json"
    )


@pytest.fixture
def located(manager, monkeypatch):
    """locate 后 codex 处于 pending-auth"""
    monkeypatch.setattr(sm_module, "find_window", lambda *a, **k: make_window())
    manager.locate_all()
    return manager


class TestStateTransition:
    def test_pending_auth_to_authorizing_to_authorized_to_wgc(self, located):
        s = located.get("codex")
        assert s is not None and s.mode == "pending-auth"

        res = located.prepare_authorize("codex")
        assert res["ok"] is True and s.mode == "authorizing"
        assert res["hint"] and "Codex" in res["hint"]

        fin = located.finish_authorize("codex")
        assert fin["ok"] is True
        assert s.authorized is True
        assert s.mode == "wgc"
        assert located.start_wgc("codex") is True, "授权成功后应能启动 WGC"

    def test_authorize_failure_downgrades_to_status_only(self, located, monkeypatch):
        def fail_trial(self_, session, timeout):
            return False, "用户拒绝/选择器取消"

        monkeypatch.setattr(SessionManager, "_trial_capture", fail_trial)
        s = located.get("codex")
        fin = located.finish_authorize("codex")
        assert fin["ok"] is False
        assert s.mode == "status-only", "拒绝应降级 status-only"
        assert s.authorized is False
        assert "授权被拒" in s.last_error


class TestPersistence:
    def test_mark_authorized_atomic_write_no_sensitive(self, manager):
        s = manager.get("codex")
        assert s is not None
        s.window = make_window()
        manager.mark_authorized("codex")

        path = manager._auth_file
        assert path.exists(), "授权成功应落盘"
        assert not path.with_name(path.name + ".tmp").exists(), "原子写不得残留 .tmp"

        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["version"] == 1
        assert len(doc["windows"]) == 1
        w = doc["windows"][0]
        assert w["app_id"] == "codex"
        assert w["window_title"] == "Codex"
        assert w["authorized"] is True
        assert isinstance(w["authorized_at"], int)

        text = path.read_text(encoding="utf-8").lower()
        for bad in ("token", "webhook", "screenshot", "command"):
            assert bad not in text, f"持久化不得含敏感信息: {bad}"

    def test_restart_loads_persisted_authorized(self, manager, monkeypatch, tmp_path):
        path = tmp_path / "authorized_windows.json"
        s = manager.get("codex")
        assert s is not None
        s.window = make_window()
        manager.mark_authorized("codex")

        # 模拟重启：新实例加载同一 auth_file
        monkeypatch.setattr(sm_module, "find_window", lambda *a, **k: make_window())
        sm2 = SessionManager(
            [MonitorTarget(app_id="codex", app_name="Codex", process_name="codex.exe")],
            tmp_path,
            auth_file=path,
        )
        s2 = sm2.get("codex")
        assert s2 is not None and s2.authorized is True, "重启后应自动恢复授权"
        sm2.locate_all()
        assert s2.mode == "wgc", "已授权窗口重启后直接 wgc"

    def test_corrupt_auth_file_tolerated(self, tmp_path):
        path = tmp_path / "authorized_windows.json"
        path.write_text("{broken json", encoding="utf-8")
        sm = SessionManager(
            [MonitorTarget(app_id="codex", app_name="Codex", process_name="codex.exe")],
            tmp_path,
            auth_file=path,
        )
        assert sm.get("codex") is not None
        assert sm.get("codex").authorized is False, "损坏文件视为未授权，不阻断启动"

    def test_missing_auth_file_tolerated(self, tmp_path):
        sm = SessionManager(
            [MonitorTarget(app_id="codex", app_name="Codex", process_name="codex.exe")],
            tmp_path,
            auth_file=tmp_path / "nonexistent.json",
        )
        assert sm.get("codex").authorized is False


class TestPrepareAuthorizeErrors:
    def test_unknown_app_40401(self, manager):
        res = manager.prepare_authorize("ghost")
        assert res["ok"] is False and res["code"] == 40401

    def test_already_authorized_40001(self, located):
        located.get("codex").authorized = True
        res = located.prepare_authorize("codex")
        assert res["code"] == 40001

    def test_in_progress_40901(self, located):
        located.get("codex").mode = "authorizing"
        res = located.prepare_authorize("codex")
        assert res["code"] == 40901

    def test_denied_requires_retry_40902(self, located):
        located.mark_denied("codex", "用户拒绝")
        res = located.prepare_authorize("codex")
        assert res["code"] == 40902

        res2 = located.prepare_authorize("codex", retry=True)
        assert res2["ok"] is True and res2["mode"] == "authorizing", "retry 应重置拒绝态"

    def test_window_missing_40402(self, manager, monkeypatch):
        monkeypatch.setattr(sm_module, "find_window", lambda *a, **k: None)
        manager.locate_all()
        res = manager.prepare_authorize("codex")
        assert res["code"] == 40402


class TestStartWgcRefusal:
    def test_start_wgc_refused_when_status_only(self, located):
        s = located.get("codex")
        assert s is not None
        located.mark_denied("codex", "超时")
        assert s.mode == "status-only"
        s.authorized = True  # 即使标记授权也不得启动（需重新授权）
        assert located.start_wgc("codex") is False
        assert "拒绝授权" in s.last_error

    def test_start_wgc_refused_when_denied(self, located):
        s = located.get("codex")
        assert s is not None
        s.authorized = True
        s.mode = "denied"
        assert located.start_wgc("codex") is False
        assert "拒绝授权" in s.last_error


class TestTrialCapture:
    def test_trial_capture_first_frame_success(self, manager, monkeypatch):
        class OkTrial:
            def __init__(self, window_title="", output_dir=None):
                self._ok, self._err = True, ""

            def run(self) -> None:
                pass  # 立即完成（首帧即停）

            def request_stop(self) -> None:
                pass

            @property
            def result(self):
                return self._ok, self._err

        monkeypatch.setattr(sm_module, "WgcTrialCapturer", OkTrial)
        s = manager.get("codex")
        assert s is not None
        s.window = make_window()
        ok, err = manager._trial_capture(s, timeout=1.0)
        assert ok is True and err == ""

    def test_trial_capture_timeout(self, manager, monkeypatch):
        class SlowTrial:
            def __init__(self, window_title="", output_dir=None):
                self.stopped = False

            def run(self) -> None:
                time.sleep(5)  # 超过 timeout → 线程未结束

            def request_stop(self) -> None:
                self.stopped = True

            @property
            def result(self):
                return False, "授权超时"

        monkeypatch.setattr(sm_module, "WgcTrialCapturer", SlowTrial)
        s = manager.get("codex")
        assert s is not None
        s.window = make_window()
        ok, err = manager._trial_capture(s, timeout=0.05)
        assert ok is False and "超时" in err


class FakeSessions:
    """orchestrator 授权流程测试用 stub"""

    def __init__(self) -> None:
        self.prepared: tuple | None = None
        self.finished: list[str] = []
        self.started: list[str] = []

    def prepare_authorize(self, app_id: str, retry: bool = False) -> dict:
        self.prepared = (app_id, retry)
        return {
            "ok": True,
            "code": 0,
            "mode": "authorizing",
            "app_id": app_id,
            "app_name": "Codex",
            "hint": "请在系统弹窗中允许捕获窗口 Codex",
        }

    def finish_authorize(self, app_id: str) -> dict:
        self.finished.append(app_id)
        return {"ok": True, "mode": "wgc", "authorized": True, "error": None}

    def start_wgc(self, app_id: str) -> bool:
        self.started.append(app_id)
        return True


def _make_orchestrator(bus: EventBus) -> Orchestrator:
    cfg = AppConfig()
    return Orchestrator(cfg, bus, client=None, analyzer=None, push=None, reminder=None)


class TestOrchestratorAuthorize:
    @pytest.mark.asyncio
    async def test_success_emits_prompt_and_result_and_starts_wgc(self):
        bus = EventBus()
        events: list[tuple[str, dict]] = []

        async def record(ev: str):
            async def handler(data: dict) -> None:
                events.append((ev, data))

            return handler

        bus.subscribe(EVT_AUTH_PROMPT, await record(EVT_AUTH_PROMPT))
        bus.subscribe(EVT_AUTH_RESULT, await record(EVT_AUTH_RESULT))

        orch = _make_orchestrator(bus)
        fake = FakeSessions()
        orch._sessions = fake  # type: ignore[assignment]

        res = await orch.authorize_capture("codex")

        assert res["ok"] is True
        assert fake.prepared == ("codex", False)
        assert fake.finished == ["codex"]
        assert fake.started == ["codex"], "授权成功必须启动 WGC"
        assert [e[0] for e in events] == [EVT_AUTH_PROMPT, EVT_AUTH_RESULT]
        prompt = events[0][1]
        assert prompt["app_id"] == "codex" and "系统弹窗" in prompt["hint"]
        result = events[1][1]
        assert result["ok"] is True and result["mode"] == "wgc"

    @pytest.mark.asyncio
    async def test_failure_emits_result_but_no_start_wgc(self):
        bus = EventBus()
        events: list[tuple[str, dict]] = []

        async def record(ev: str):
            async def handler(data: dict) -> None:
                events.append((ev, data))

            return handler

        bus.subscribe(EVT_AUTH_PROMPT, await record(EVT_AUTH_PROMPT))
        bus.subscribe(EVT_AUTH_RESULT, await record(EVT_AUTH_RESULT))

        orch = _make_orchestrator(bus)

        class FailSessions(FakeSessions):
            def finish_authorize(self, app_id: str) -> dict:
                self.finished.append(app_id)
                return {
                    "ok": False,
                    "mode": "status-only",
                    "authorized": False,
                    "error": "授权被拒: 授权超时",
                }

        fake = FailSessions()
        orch._sessions = fake  # type: ignore[assignment]

        res = await orch.authorize_capture("codex")

        assert res["ok"] is False and res["mode"] == "status-only"
        assert fake.started == [], "授权失败不得启动 WGC"
        assert events[-1][0] == EVT_AUTH_RESULT
        assert events[-1][1]["ok"] is False

    @pytest.mark.asyncio
    async def test_prepare_error_skips_events(self):
        bus = EventBus()
        events: list[tuple[str, dict]] = []

        async def record(ev: str):
            async def handler(data: dict) -> None:
                events.append((ev, data))

            return handler

        bus.subscribe(EVT_AUTH_PROMPT, await record(EVT_AUTH_PROMPT))
        bus.subscribe(EVT_AUTH_RESULT, await record(EVT_AUTH_RESULT))

        orch = _make_orchestrator(bus)

        class UnknownSessions(FakeSessions):
            def prepare_authorize(self, app_id: str, retry: bool = False) -> dict:
                return {"ok": False, "code": 40401, "error": f"未知 app_id: {app_id}"}

        fake = UnknownSessions()
        orch._sessions = fake  # type: ignore[assignment]

        res = await orch.authorize_capture("ghost")

        assert res["code"] == 40401
        assert events == [], "校验失败不得下发 auth 事件"


class TestWsAuthEvents:
    @pytest.mark.asyncio
    async def test_ws_broadcast_contract(self):
        from app.api.routes_ws import WsHub

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_json(self, msg: dict) -> None:
                self.sent.append(msg)

        bus = EventBus()
        hub = WsHub(bus)
        ws = FakeWS()
        hub._connections.add(ws)  # type: ignore[arg-type]

        prompt_data = {"app_id": "codex", "app_name": "Codex", "hint": "hint"}
        result_data = {"app_id": "codex", "ok": False, "mode": "status-only", "error": "x"}
        await bus.emit(EVT_AUTH_PROMPT, prompt_data)
        await bus.emit(EVT_AUTH_RESULT, result_data)

        assert ws.sent[0] == {"type": "event", "event": "auth_prompt", "data": prompt_data}
        assert ws.sent[1] == {"type": "event", "event": "auth_result", "data": result_data}
