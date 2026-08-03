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

FrameCallback = Callable[[Path], None]


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
        try:
            capture = WindowsCapture(
                window_name=self._window_title,
                cursor_capture=None,
                draw_border=None,
            )

            @capture.event
            def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
                self._frame_count += 1
                path = self._output_dir / f"frame_{self._frame_count}.png"
                frame.save_as_image(str(path))
                self._last_frame_path = path
                self._last_frame_at = time.time()
                if self._on_frame:
                    self._on_frame(path)

            capture.start()  # 阻塞直到 stop
        except Exception:  # noqa: BLE001
            logger.exception("wgc capture thread crashed: %s", self._window_title)
            self._running = False

    def stop(self) -> None:
        self._running = False
        # windows-capture 内部 stop 由线程退出处理；此处标记后线程自然结束

    def snapshot(self) -> tuple[Path | None, float]:
        """返回 (最近一帧路径, 捕获时间戳)；尚无帧返回 (None, 0.0)。

        时间戳用于调用方判断"是否有新帧"：orchestrator 轮询只消费新帧，
        空帧不再返回旧路径（否则会一直拿旧图分析）。
        """
        return self._last_frame_path, self._last_frame_at
