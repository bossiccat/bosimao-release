"""WGC 捕获封装（windows-capture 2.0.0）+ DXGI 兜底

PoC B2 已验证 API 形态：
    WindowsCapture(window_name=..., cursor_capture=None, draw_border=None)
    @capture.event on_frame_arrived(frame, capture_control)
    frame.save_as_image(path)
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

try:
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
except ImportError:
    WindowsCapture = None  # type: ignore[assignment]

try:
    import pythoncom  # 模块级导入：若在线程内 import 会触发隐式 STA 初始化
except ImportError:
    pythoncom = None  # type: ignore[assignment]

FrameCallback = Callable[[Path], None]

# ADR-010 文件堆积对策：每窗口会话最多保留最近 MAX_FRAMES 帧，超限删最旧
MAX_FRAMES = 200


def _init_com_apartment() -> None:
    """WGC（WinRT/COM）必须在 MTA 公寓线程初始化。

    根因（冒烟实测）：windows-capture 是 WinRT 应用，若在未初始化 COM 的
    daemon 线程创建 WindowsCapture → Rust panic "Failed to initialize WinRT"
    → 捕获线程崩溃，后端事件循环被连带冻结。显式 CoInitializeEx(MTA) 修复。
    PoC B2 在脚本主线程跑（隐式可初始化）未暴露此问题。
    注意：pythoncom 必须模块级导入——线程内 import 会先隐式 STA 初始化，
    再 CoInitializeEx(MTA) 报 RPC_E_CHANGED_MODE 被吞 → WinRT 仍失败。
    """
    if pythoncom is None:
        return
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001  已初始化/非 Windows 环境跳过
        logger.debug("COM MTA 初始化跳过", exc_info=True)


class WgcCapturer:
    """单窗口 WGC 捕获会话（每被监控窗口一个实例）"""

    def __init__(
        self,
        window_title: str,
        output_dir: Path,
        on_frame: FrameCallback | None = None,
    ) -> None:
        if WindowsCapture is None:
            raise RuntimeError("windows-capture 未安装")
        self._window_title = window_title
        self._output_dir = output_dir
        self._on_frame = on_frame
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_frame_path: Path | None = None
        self._last_frame_at: float = 0.0
        self._frame_count = 0

    def start(self) -> None:
        """启动捕获线程（阻塞运行，放独立线程）"""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"wgc-{self._window_title}")
        self._thread.start()
        logger.info("wgc capture started: window=%s", self._window_title)

    def _run(self) -> None:
        _init_com_apartment()  # WinRT/COM MTA：无此步 WGC 线程崩溃并冻结事件循环
        try:
            capture = WindowsCapture(
                window_name=self._window_title,
                cursor_capture=None,
                draw_border=None,
            )

            @capture.event
            def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
                self._handle_frame(frame)
                # stop 请求：结束底层捕获，让 capture.start() 返回、线程干净退出
                # （不这样做线程会泄漏，同一窗口二次捕获会冲突/阻塞）
                if not self._running:
                    try:
                        capture_control.stop()
                    except Exception:  # noqa: BLE001
                        logger.debug("wgc stop ack failed: %s", self._window_title)

            @capture.event
            def on_closed(capture_control: InternalCaptureControl):
                # windows-capture 2.0.0 要求必须注册 on_closed，否则 start() 抛异常
                logger.info("wgc capture closed: window=%s", self._window_title)
                self._running = False

            capture.start_free_threaded()  # 阻塞直到 stop/on_closed（free-threaded：释放 GIL，避免冻结 asyncio 事件循环）
        except Exception:  # noqa: BLE001
            logger.exception("wgc capture thread crashed: %s", self._window_title)
            self._running = False

    def _handle_frame(self, frame) -> None:
        """保存帧 → 清理超限旧帧 → 记录最近帧 → 通知回调"""
        self._frame_count += 1
        path = self._output_dir / f"frame_{self._frame_count}.png"
        frame.save_as_image(str(path))
        self._last_frame_path = path
        self._last_frame_at = time.time()
        self._prune_old_frames()
        if self._on_frame:
            self._on_frame(path)

    def _prune_old_frames(self) -> None:
        """超过 MAX_FRAMES 时删除最旧帧（帧序号单调递增，最旧=最小序号）。

        ADR-010 文件堆积对策：每会话磁盘最多保留最近 MAX_FRAMES 帧。
        删除的是 frame_1..frame_{excess}，不影响 _last_frame_path（最近帧）。
        """
        if self._frame_count <= MAX_FRAMES:
            return
        excess = self._frame_count - MAX_FRAMES
        removed = 0
        for n in range(1, excess + 1):
            path = self._output_dir / f"frame_{n}.png"
            try:
                if path.exists():
                    path.unlink()
                    removed += 1
            except OSError:
                logger.warning("prune failed to remove: %s", path)
        if removed:
            logger.info(
                "wgc pruned %d old frames: window=%s total=%d",
                removed,
                self._window_title,
                self._frame_count,
            )

    def cleanup(self) -> None:
        """删除本会话全部帧文件（会话 stop 时由 SessionManager 调用）"""
        if not self._output_dir.exists():
            return
        removed = 0
        for path in self._output_dir.glob("frame_*.png"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("cleanup failed to remove: %s", path)
        if removed:
            logger.info(
                "wgc cleanup removed %d frames: window=%s",
                removed,
                self._window_title,
            )

    def stop(self) -> None:
        self._running = False
        # windows-capture 内部 stop 由线程退出处理；此处标记后线程自然结束

    def is_running(self) -> bool:
        """捕获会话是否存活。

        on_closed / 线程异常时置 False——orchestrator 据此检测 WGC 崩溃
        （Trae 最小化必崩：进程 exit 1 后此处返回 False，触发重建）。
        """
        return self._running

    def snapshot(self) -> tuple[Path | None, float]:
        """返回 (最近一帧路径, 捕获时间戳)；尚无帧返回 (None, 0.0)。

        时间戳用于调用方判断"是否有新帧"：orchestrator 轮询只消费新帧，
        空帧不再返回旧路径（否则会一直拿旧图分析）。
        """
        return self._last_frame_path, self._last_frame_at


class WgcTrialCapturer:
    """一次性试捕获（WGC 授权探测）：启动后首个有效帧即停。

    与持久会话 WgcCapturer 的关键区别：首帧到达时立即 capture_control.stop()，
    底层捕获线程干净退出——不会与随后 start_wgc 的真实会话对同一窗口双重捕获
    （PoC B2 冒烟实测：双捕获会导致后端事件循环阻塞/挂起）。

    授权语义：首次对未授权窗口启动 WGC，Windows 自动弹系统选择器；
    出首帧 = 授权成功（POC-002：四窗口均自动授权）。
    """

    def __init__(self, window_title: str, output_dir: Path) -> None:
        if WindowsCapture is None:
            raise RuntimeError("windows-capture 未安装")
        self._window_title = window_title
        self._output_dir = output_dir
        self._stop_requested = False
        self._ok = False
        self._error = "授权超时"
        self._frame_count = 0

    def request_stop(self) -> None:
        """尽力停止（下一帧到达时关闭底层捕获）"""
        self._stop_requested = True

    def run(self) -> None:
        """同步运行：阻塞到首帧（自动 stop）或自然关闭。"""
        _init_com_apartment()  # WinRT/COM MTA：无此步 WGC 线程崩溃
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            capture = WindowsCapture(
                window_name=self._window_title,
                cursor_capture=None,
                draw_border=None,
            )

            @capture.event
            def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
                self._frame_count += 1
                if self._frame_count == 1:
                    self._ok = True
                    self._error = ""
                if self._stop_requested or self._ok:
                    # 首帧即停 / 外部 stop 请求 → 结束底层捕获，线程干净退出
                    try:
                        capture_control.stop()
                    except Exception:  # noqa: BLE001
                        pass

            @capture.event
            def on_closed(capture_control: InternalCaptureControl):
                logger.info("wgc trial closed: window=%s", self._window_title)

            capture.start_free_threaded()  # 阻塞直到 stop（free-threaded：释放 GIL，避免冻结事件循环）
        except Exception as e:  # noqa: BLE001
            self._ok = False
            self._error = f"系统选择器不可用: {e}"

    @property
    def result(self) -> tuple[bool, str]:
        return self._ok, self._error
