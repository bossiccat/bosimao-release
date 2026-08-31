r"""契约：Load-Env 必须以 UTF-8 解码 .env（PS5.1 GBK mojibake 根治）。

背景（AC-1 P1 / Load-Env 乱码系统性排查，2026-09-01）：
    .env 为 UTF-8 无 BOM（实测首字节 b'# ='）。scripts/jax-services.ps1 :: Load-Env
    原实现 `Get-Content $envFile` 无 -Encoding 参数，Windows PowerShell 5.1 按
    系统 ANSI（GBK）解码 —— .env 中 4 条含中文「监视app」的绝对路径
    （SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_CA_FILE / RTC_BRIDGE_CLIENT_CERT_FILE /
    RTC_BRIDGE_CLIENT_KEY_FILE）全部以 mojibake 注入子进程。

    后果分级：
    - Brain 回调（main.py）已有存在性校验 + 仓库相对兜底（244552c）→ 被掩盖；
    - 控制面 mTLS 客户端（ack_reporter/redemption，惰性构建）无兜底 →
      ssl.create_default_context(cafile=<乱码路径>) 在首次终止上报时抛异常 —— 地雷。

    根 fix = Load-Env 读文件显式 `-Encoding UTF8`（PS5.1 对无 BOM/带 BOM UTF-8
    均正确解码）。本契约两条腿：
    1. 静态：Load-Env 函数体必须显式声明 -Encoding UTF8；
    2. 实测（真机通道）：用 PowerShell AST 从真实脚本提取 Load-Env 并在本机执行，
       断言 4 条中文路径逐字（verbatim）注入、目标文件真实存在、无 U+FFFD。

RED 基线：无 -Encoding UTF8 的历史实现上两条断言均失败。
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "jax-services.ps1"

# .env 中经 Load-Env 注入、且值含中文的 4 条路径型变量（mojibake 风险面）。
_PATH_VARS = (
    "SSL_CERT_FILE",
    "RTC_BRIDGE_CONTROL_PLANE_CA_FILE",
    "RTC_BRIDGE_CLIENT_CERT_FILE",
    "RTC_BRIDGE_CLIENT_KEY_FILE",
)


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^function\s+{re.escape(name)}\s*\{{(?P<body>.*?)^\}}\s*$",
        source,
    )
    assert match is not None, f"PowerShell function not found: {name}"
    return match.group("body")


def test_load_env_declares_utf8_decoding() -> None:
    """静态腿：Load-Env 必须显式 -Encoding UTF8（禁止依赖系统 ANSI 默认解码）。"""
    source = _SCRIPT.read_text(encoding="utf-8-sig")
    body = _function_body(source, "Load-Env")
    get_content_lines = [ln for ln in body.splitlines() if "Get-Content" in ln]
    assert get_content_lines, "Load-Env must read .env via Get-Content"
    assert any(
        re.search(r"-\s*Encoding\s+UTF8", ln, re.IGNORECASE) for ln in get_content_lines
    ), (
        "Load-Env Get-Content must declare -Encoding UTF8: on Windows PowerShell 5.1 "
        "the default decodes UTF-8 (no BOM) .env as system ANSI/GBK, mojibaking every "
        "non-ASCII path (SSL_CERT_FILE, RTC_BRIDGE_CONTROL_PLANE_*_FILE, ...)"
    )


def test_load_env_injects_chinese_paths_verbatim() -> None:
    """实测腿：真实执行 Load-Env，4 条中文路径逐字注入且目标文件存在（无 mojibake）。"""
    # PS 侧：AST 提取真实 Load-Env（不 dot-source 整个脚本，避免副作用），执行后输出 JSON。
    ps_script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{_SCRIPT}', [ref]$tokens, [ref]$parseErrors)
$fn = $ast.FindAll({{param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'Load-Env'}}, $true) |
    Select-Object -First 1
if (-not $fn) {{ Write-Error 'Load-Env not found in script'; exit 3 }}
$Root = '{_ROOT}'
Invoke-Expression $fn.Extent.Text
Load-Env
@{{
{os.linesep.join(f'  {name.lower()} = $env:{name}' for name in _PATH_VARS)}
}} | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"Load-Env execution failed (exit {proc.returncode}): {proc.stderr[:500]}"
    )
    values = _parse_flat_json(proc.stdout)

    for name in _PATH_VARS:
        value = values.get(name.lower(), "")
        assert value, f"{name} must be injected by Load-Env (got empty)"
        assert "\ufffd" not in value, f"{name} contains U+FFFD replacement chars"
        assert "监视app" in value, (
            f"{name} is mojibaked (no 监视app substring): {value!r} — "
            "Load-Env decoded UTF-8 .env as GBK"
        )
        assert Path(value).is_file(), (
            f"{name} points to a nonexistent file: {value!r}"
        )


def _parse_flat_json(stdout: str) -> dict:
    import json

    text = stdout.strip()
    # 容忍 PS 输出前导杂散行：取最后一个以 { 开头的行块。
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].lstrip().startswith("{"):
            return json.loads(" ".join(lines[idx:]))
    raise AssertionError(f"no JSON object in Load-Env output: {text[:300]!r}")
