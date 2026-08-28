"""Supervised, read-only Hermes worker process runner."""
from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from .agent_thread_registry import AgentThreadRegistry
from .hermes_worker_profiles import (
    DEFAULT_PROFILE,
    REQUIRED_BINDING_KEYS,
    WORKER_PROFILES,
    binding_matches,
)


class HermesWorkerRunner:
    """Run only an allowlisted Hermes probe and persist its lifecycle."""

    def __init__(
        self,
        registry: AgentThreadRegistry,
        hermes_bin: str = "hermes",
        command_factory: Callable[[str], Sequence[str]] | None = None,
        profile: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if command_factory is not None and (profile or DEFAULT_PROFILE) != DEFAULT_PROFILE:
            raise ValueError(
                "command_factory 仅供测试注入默认 probe_help profile；"
                "受限 profile 不允许命令覆盖。"
            )
        self._registry = registry
        self._hermes_bin = hermes_bin
        # 全量命令覆盖仅限测试缝隙（默认 profile）；生产装配不得传入。
        self._command_factory = command_factory
        self._profile = profile or DEFAULT_PROFILE
        # 硬超时：到期 terminate→kill，线程标记 failed，防止挂死进程无限占用。
        self._timeout_seconds = max(0.1, float(timeout_seconds))
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
        args = WORKER_PROFILES.get(self._profile)
        if args is None:
            return None
        return (binary, *args)

    def _verify_command_binding(self, thread_id: str) -> str | None:
        """校验审批命令 payload 携带的 profile 绑定与 runner 配置一致。

        返回错误摘要（无绑定/篡改/不匹配），None 表示通过。
        仅默认 profile 的测试缝隙（command_factory 注入）跳过校验。
        """
        if self._command_factory is not None:
            return None
        db_row = self._registry.last_command_payload(thread_id)
        if not db_row:
            return "审批命令缺少 profile 绑定，后台 Worker 拒绝启动。"
        missing = [k for k in REQUIRED_BINDING_KEYS if k not in db_row]
        if missing:
            return f"审批命令绑定不完整（缺 {','.join(missing)}），后台 Worker 拒绝启动。"
        if not binding_matches(str(db_row["profile"]), tuple(db_row["command_argv"])):
            return "审批命令绑定与 allowlist 不一致，后台 Worker 拒绝启动。"
        if str(db_row["profile"]) != self._profile:
            return "审批命令绑定的 profile 与 runner 配置不一致，后台 Worker 拒绝启动。"
        return None

    async def start(self, thread_id: str) -> dict:
        thread = self._registry.get(thread_id)
        if thread is None:
            return {"error": "thread_not_found"}

        binding_error = self._verify_command_binding(thread_id)
        if binding_error is not None:
            return self._registry.update(thread_id, "failed", binding_error) or {
                "error": "thread_not_found"
            }

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
        timed_out = False
        try:
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                exit_code = process.returncode
        finally:
            self._processes.pop(thread_id, None)
            self._running_events.pop(thread_id, None)

        current = self._registry.get(thread_id)
        if current is not None and current["status"] == "cancelled":
            return current
        if timed_out:
            return self._registry.update(
                thread_id,
                "failed",
                f"后台 Worker 执行超时（>{self._timeout_seconds:g}s），进程已终止。",
            ) or {"error": "thread_not_found"}
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

    def start_sync(self, thread_id: str) -> dict:
        """Synchronous start for tests and control-plane probes."""
        return asyncio.run(self.start(thread_id))

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
