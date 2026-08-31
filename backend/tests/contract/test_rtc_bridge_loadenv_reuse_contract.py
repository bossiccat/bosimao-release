r"""契约：Start-RtcBridgeService 启动 rtc_bridge 前必须复用既有 Load-Env（或等价 .env 加载）。

背景（AC-1 / BRAIN_API_URL 装配）：
    backend/rtc_bridge/main.py 在 main_async() 中读取 os.environ["BRAIN_API_URL"]，
    缺失则 voice intent 路由静默降级为 disabled。启动链为
    scripts/jax-services.ps1 :: Start-RtcBridgeService → Start-Process pythonw -m rtc_bridge.main，
    子进程环境完全继承自父 PowerShell 进程，因此父进程必须在 Start-Process 之前
    调用既有 Load-Env（$Root\.env → 进程环境）或等价加载，否则 BRAIN_API_URL 永远为空。

RED 基线：引入 Start-RtcBridgeService 的提交 644ad33（TRTC phase A/B）中该函数
    未调用 Load-Env —— 本测试在该版本上必须失败（可用环境变量 JAX_SERVICES_PS1
    指向历史版本文件验证 RED 敏感性，默认指向工作区脚本）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
# 默认校验工作区脚本；允许用 JAX_SERVICES_PS1 指向其它版本（仅用于 RED 敏感性演示）。
_SCRIPT = Path(os.environ.get("JAX_SERVICES_PS1") or (_ROOT / "scripts" / "jax-services.ps1"))


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^function\s+{re.escape(name)}\s*\{{(?P<body>.*?)^\}}\s*$",
        source,
    )
    assert match is not None, f"PowerShell function not found: {name}"
    return match.group("body")


def test_start_rtc_bridge_service_reuses_load_env_before_launch() -> None:
    source = _SCRIPT.read_text(encoding="utf-8-sig")
    body = _function_body(source, "Start-RtcBridgeService")

    launch_offset = body.find("Start-Process")
    assert launch_offset >= 0, "Start-RtcBridgeService must launch rtc_bridge via Start-Process"
    prologue = body[:launch_offset]

    reuses_shared_load_env = re.search(r"(?mi)^\s*Load-Env\b", prologue) is not None
    equivalent_env_loading = (
        re.search(r"(?i)Join-Path\s+\$Root\s+[\"']\.env[\"']", prologue) is not None
        and "SetEnvironmentVariable" in prologue
    )

    assert reuses_shared_load_env or equivalent_env_loading, (
        "Start-RtcBridgeService must call Load-Env (or equivalent .env loading) before "
        "Start-Process so the rtc_bridge child process inherits BRAIN_API_URL"
    )


def test_shared_load_env_function_injects_process_environment() -> None:
    """被复用的 Load-Env 必须真实地把 $Root\\.env 注入进程环境（子进程可继承）。"""
    source = _SCRIPT.read_text(encoding="utf-8-sig")
    body = _function_body(source, "Load-Env")

    assert re.search(r"(?i)Join-Path\s+\$Root\s+[\"']\.env[\"']", body), (
        "Load-Env must read $Root\\.env"
    )
    assert "SetEnvironmentVariable" in body, (
        "Load-Env must inject into the process environment via "
        "[Environment]::SetEnvironmentVariable so Start-Process children inherit it"
    )
