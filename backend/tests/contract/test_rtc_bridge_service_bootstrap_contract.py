from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JAX_SERVICES = ROOT / "scripts" / "jax-services.ps1"


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^function\s+{re.escape(name)}\s*\{{(?P<body>.*?)^\}}\s*$",
        source,
    )
    assert match is not None, f"PowerShell function not found: {name}"
    return match.group("body")


def test_rtc_bridge_loads_env_before_starting_process() -> None:
    source = JAX_SERVICES.read_text(encoding="utf-8-sig")
    body = _function_body(source, "Start-RtcBridgeService")
    launch_offset = body.find("Start-Process")

    assert launch_offset >= 0, "Start-RtcBridgeService must launch rtc_bridge"
    before_launch = body[:launch_offset]
    reuses_load_env = re.search(r"(?mi)^\s*Load-Env\s*(?:#.*)?$", before_launch) is not None
    loads_dotenv_equivalently = (
        re.search(r"(?i)Join-Path\s+\$Root\s+[\"']\.env[\"']", before_launch) is not None
        and "SetEnvironmentVariable" in before_launch
    )

    assert reuses_load_env or loads_dotenv_equivalently, (
        "Start-RtcBridgeService must load .env before Start-Process so rtc_bridge.main "
        "inherits BRAIN_API_URL"
    )
