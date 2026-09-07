"""Contracts for PyInstaller onefile backend lifecycle ownership."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "jax-services.ps1"


def _backend_body() -> str:
    source = SCRIPT.read_text(encoding="utf-8-sig")
    match = re.search(
        r"(?ms)^function Start-BackendService\s*\{(?P<body>.*?)^\}\s*$",
        source,
    )
    assert match, "Start-BackendService must be present"
    return match.group("body")


def test_success_records_listener_pid_after_owner_check() -> None:
    body = _backend_body()
    # 启动成功路径：owner 核对通过 → 单实例确认 → 落 pidfile（监听者 PID）→ return。
    # 「就绪」日志只在 owner.Match==true 且 known-contains 校验后出现，锚定它即隐含顺序约束。
    success = re.search(
        r"(?ms)\[backend\]\[ok\] 就绪.*?Set-PidFile\s+\"backend\"\s+\$listener.*?return \$true",
        body,
    )
    assert success, (
        "startup success must persist the :8000 listener PID, not the "
        "Start-Process bootloader PID"
    )
    # 幂等采纳路径同样必须落监听者 PID（$cur = Get-PortPid，即 :8000 监听者）
    adopt = re.search(r"Set-PidFile\s+\"backend\"\s+\$cur", body)
    assert adopt, "idempotent adopt path must persist the listener PID too"


def test_startup_failure_paths_clean_the_entire_onefile_tree() -> None:
    body = _backend_body()
    # 锚定语义（[backend][x] 前缀 + 分支文案），不锚定旧实现文本 Stop-Process——
    # 修复本身就是把 Stop-Process 换成 Stop-BackendProcesses，锚旧文本会让正确修复无法转绿
    timeout_block = re.search(
        r"(?ms)\[backend\]\[x\].*?90s.*?(?P<block>Stop-BackendProcesses.*?return \$false)",
        body,
    )
    assert timeout_block, "90s startup timeout branch must remain explicit"
    assert "Stop-BackendProcesses" in timeout_block.group("block"), (
        "startup timeout must terminate the onefile parent/child tree"
    )

    non_owner_block = re.search(
        r"(?ms)\[backend\]\[x\] 端口被非预期进程占用.*?(?P<block>Stop-BackendProcesses.*?return \$false)",
        body,
    )
    assert non_owner_block, "non-owner startup failure branch must remain explicit"
    assert "Stop-BackendProcesses" in non_owner_block.group("block"), (
        "non-owner startup failure must terminate the onefile parent/child tree"
    )


def test_startup_success_allows_only_one_logical_onefile_instance() -> None:
    body = _backend_body()
    assert "if ($allBackend.Count -le 2)" in body
    assert "known -contains $listener" in body
    assert "Set-PidFile \"backend\" $listener" in body
