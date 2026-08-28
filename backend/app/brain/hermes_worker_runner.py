"""Supervised, read-only Hermes worker process runner."""
from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from .agent_thread_registry import AgentThreadRegistry


# 服务端锁定的只读 profile allowlist。任何新 profile 必须先过安全审查再入表。
_WORKER_PROFILES: dict[str, tuple[str, ...]] = {
    # 默认安全模式：纯 CLI 帮助，零网络、零副作用。
    "probe_help": ("--help",),
    # 受控只读 DeepSeek 推理：显式 model+provider 路由（隐式 provider 推断已证实不可靠，
    # 会命中 HTTP 401），空 toolsets 禁用一切工具副作用，oneshot 单轮、提示词固定为 ping。
    "deepseek_readonly": (
        "-z",
        "--model", "deepseek-v4-flash-0731",
        "--provider", "kkdmx",
        "--toolsets", "",
        "ping",
    ),
}

_DEFAULT_PROFILE = "probe_help"


class HermesWorkerRunner:
    """Run only an allowlisted Hermes probe and persist its lifecycle."""

    def __init__(
        self,
        registry: AgentThreadRegistry,
        hermes_bin: str = "hermes",
        command_factory: Callable[[str], Sequence[str]] | None = None,
        profile: str | None = None,
    ) -> None:
        if command_factory is not None and (profile or _DEFAULT_PROFILE) != _DEFAULT_PROFILE:
            raise ValueError(
                "command_factory 仅供测试注入默认 probe_help profile；"
                "受限 profile 不允许命令覆盖。"
            )
        self._registry = registry
        self._hermes_bin = hermes_bin
        # 全量命令覆盖仅限测试缝隙（默认 profile）；生产装配不得传入。
        self._command_factory = command_factory
        self._profile = profile or _DEFAULT_PROFILE
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._running_events: dict[str, asyncio.Event] = {}

    def _resolve_binary(self) -> str | None:
        candidate = Path(self._hermes_bin)
        if candidate.is_file():
            return str(candidate)
        return shutil.which(self._hermes_bin)

    def _build_command(self, binary: str) -> tuple[str, ...] | None:
        """按 allowlist 组装命令；未知 profile 返回 None（启动阶段拒绝）。"""
        if self._command_factory is not None:
            return tuple(self._command_factory(binary))
        args = _WORKER_PROFILES.get(self._profile)
        if args is None:
            return None
        return (binary, *args)

    async def start(self, thread_id: str) -> dict:
        thread = self._registry.get(thread_id)
        if thread is None:
            return {"error": "thread_not_found"}

        binary = self._resolve_binary()
        if binary is None:
            return self._registry.update(
                thread_id,
                "failed",
                "Hermes CLI 不可用，后台 Worker 未启动。",
            ) or {"error": "thread_not_found"}

        command = tuple(self._command_factory(binary)) if self._build_command(binary) else ()
        if not command:
            return self._registry.update(
                thread_id,
                "failed",
                f"Worker profile 配置无效（{self._profile}），后台 Worker 未启动。",
            ) or {"error": "thread_not_found"}

        current = self._registry.get(thread_id)
        if current is None:
            return {"error": "thread_not_found"}
        if current["status"] == "queued":
            if self._registry.claim_queued(thread_id) is None:
                return self._registry.get(thread_id) or {"error": "thread_not_found"}
        elif current["status"] != "running":
            return current
        running_event = self._running_events.setdefault(thread_id, asyncio.Event())
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            self._running_events.pop(thread_id, None)
            return self._registry.update(
                thread_id,
                "failed",
                "Hermes CLI 启动失败，后台 Worker 未执行。",
            ) or {"error": "thread_not_found"}

        self._processes[thread_id] = process
        running_event.set()
        try:
            exit_code = await process.wait()
        finally:
            self._processes.pop(thread_id, None)
            self._running_events.pop(thread_id, None)

        current = self._registry.get(thread_id)
        if current is not None and current["status"] == "cancelled":
            return current
        if exit_code == 0:
            return self._registry.update(
                thread_id,
                "completed",
                "后台 Worker 已完成只读 Hermes 探测。",
            ) or {"error": "thread_not_found"}
        return self._registry.update(
            thread_id,
            "failed",
            f"后台 Worker 的 Hermes 探测失败（退出码 {exit_code}）。",
        ) or {"error": "thread_not_found"}

    async def wait_until_running(self, thread_id: str, timeout: float = 5.0) -> None:
        event = self._running_events.get(thread_id)
        if event is None:
            await asyncio.sleep(0)
            event = self._running_events.get(thread_id)
        if event is None:
            raise RuntimeError("Worker 未进入运行状态")
        await asyncio.wait_for(event.wait(), timeout=timeout)

    async def cancel(self, thread_id: str) -> bool:
        process = self._processes.get(thread_id)
        if process is None or process.returncode is not None:
            return False
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        self._registry.update(thread_id, "cancelled", "后台 Worker 已取消。")
        return True
