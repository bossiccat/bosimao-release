r"""契约：三条活跃通道读取 .env 必须显式以 UTF-8 解码（PS5.1 GBK mojibake 根治）。

背景（AC-1 P1 / Load-Env 乱码系统性排查，2026-09-01）：
    .env 为 UTF-8 无 BOM（实测首 3 字节 b'# ='）。Windows PowerShell 5.1 的
    Get-Content 默认按系统 ANSI（中文机器 = GBK / CP936）解码，把 .env 中 4 条
    含中文「监视app」的绝对路径（SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_CA_FILE /
    RTC_BRIDGE_CLIENT_CERT_FILE / RTC_BRIDGE_CLIENT_KEY_FILE）全部以 mojibake
    注入子进程。

    后果分级：
    - Brain 回调（main.py）已有存在性校验 + 仓库相对兜底（244552c）→ 被掩盖；
    - 控制面 mTLS 客户端（ack_reporter/redemption，惰性构建）无兜底 →
      ssl.create_default_context(cafile=<乱码路径>) 在首次终止上报时抛异常 —— 地雷。

    根 fix = 读 .env 的 Get-Content 显式 `-Encoding UTF8`（PS5.1 对无 BOM/带 BOM
    的 UTF-8 均能正确解码）。

覆盖的三条活跃通道（`_CHANNELS`）。它们在文档中被明文记载为启动方式，任一漏改
都会把乱码路径灌进进程环境：
    1. scripts/jax-services.ps1 —— 具名函数 `function Load-Env { ... }`
       （提交 640726d 修复；服务编排通道）
    2. scripts/start-relay.ps1  —— 内联 if 语句：依赖外部 `$Root` / `$envFile`
       （docs/OPS-002-relay-deploy.md:83-84、docs/OPS-003-live-test.md:46 联调启动）
    3. scripts/dev.ps1          —— 内联 if 语句，形态为「赋值 + Set-Item」，依赖 `$Root`
       （docs/SPEC.md:134 一键启动；经 Start-Process 传 backend、经 npm run tauri dev
        传 Tauri -> sidecar 全链继承）

本契约对每条通道跑两条腿：
    1. 静态：读取 .env 的 Get-Content 语句必须显式声明 -Encoding UTF8；
    2. 实测（真机通道）：用 PowerShell AST（ParseFile + FindAll）从真实脚本里
       抽出真实的 env 加载片段并 Invoke-Expression 执行，断言 4 条中文路径
       逐字（verbatim）注入、目标文件真实存在、无 GBK mojibake 特征串。

    实测腿刻意不使用正则替换/模拟：三条脚本的写法互不相同（具名函数 / 内联
    管道 / 赋值 + Set-Item），统一靠 AST 定位「含 Get-Content 且写 Env: 的
    if 语句」或「Load-Env 函数定义」，再把 Extent.Text 交给 Invoke-Expression，
    执行的是脚本里的一等公民代码；注释里出现 -Encoding UTF8 骗不过它。

关于 U+FFFD（P2-C）：原实现曾断言 `"\ufffd" not in value`，该断言对本场景是死
断言。实测（2026-09-01，PS 5.1.26100）："监视app" 的 UTF-8 字节
e7 9b 91 e8 a7 86 61 70 70 被 CP936 解码为 "鐩戣" + 私用区字符 U+E74B + "app"，
全程**不产生 U+FFFD**（CP936 把无映射的双字节落到 PUA，而 Python 的 'gbk'
codec 会产出 U+FFFD —— 二者行为不同，不要据 Python codec 推断 PS 行为）。
因此改用 GBK 特征串 "鐩戣" 的否定断言。

RED 基线（已实测）：临时副本里摘掉三条通道的 -Encoding UTF8 后，本文件
6 项全部失败（3 通道 × 2 条腿），实测腿命中
SSL_CERT_FILE = 'C:\Users\Administrator\WorkBuddy\鐩戣\ue74bapp\certs\ca.crt'。
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

# .env 中经 env 加载注入、且值含中文的 4 条路径型变量（mojibake 风险面）。
_PATH_VARS = (
    "SSL_CERT_FILE",
    "RTC_BRIDGE_CONTROL_PLANE_CA_FILE",
    "RTC_BRIDGE_CLIENT_CERT_FILE",
    "RTC_BRIDGE_CLIENT_KEY_FILE",
)

# GBK/CP936 解码 UTF-8 中文的典型产物（"监视" → "鐩戣"）。见模块 docstring P2-C。
_GBK_MOJIBAKE_MARKERS = ("鐩戣",)


@dataclass(frozen=True)
class _Channel:
    """一条 .env 注入通道：脚本路径 + 该脚本内 env 加载代码的抽取方式。"""

    rel: str
    # "function"  = env 加载位于具名函数内（取 FunctionDefinitionAst）
    # "statement" = env 加载是内联 if 语句（取 IfStatementAst）
    mode: str
    target: str = ""  # mode="function" 时的函数名
    preamble: tuple = ()  # 片段执行前必须就位的词法变量（脚本自身的调用契约）


_CHANNELS = (
    _Channel(
        rel="scripts/jax-services.ps1",
        mode="function",
        target="Load-Env",
    ),
    _Channel(
        rel="scripts/start-relay.ps1",
        mode="statement",
        # 该脚本在 if 之前定义 $envFile = Join-Path $Root ".env"；抽出的片段依赖它
        # 在作用域内，故在此复刻（不执行前面的端口幂等检查与中继启动逻辑）。
        preamble=("$envFile = Join-Path $Root '.env'",),
    ),
    _Channel(
        rel="scripts/dev.ps1",
        mode="statement",
    ),
)

_PS_HEADER = r"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$tokens = $null; $parseErrors = $null"""

# AST 抽取：具名函数。取 Extent.Text 原样执行，不 dot-source 整个脚本（避免副作用）。
_PS_FIND_FUNCTION = r"""$found = @($ast.FindAll({
    param($a)
    $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $name
}, $true))
if ($found.Count -ne 1) {
    Write-Error "expected exactly 1 function '$name' in $rel, got $($found.Count)"
    exit 5
}
Invoke-Expression $found[0].Extent.Text
& $name"""

# AST 抽取：内联 if 语句。取「含 Get-Content 且写 Env:/.env/SetEnvironmentVariable」
# 的最小 if 块（最小 = Sort-Object 按文本长度升序），覆盖 start-relay 的内联管道与
# dev.ps1 的「赋值 + Set-Item」两种形态。
_PS_FIND_STATEMENT = r"""$found = @($ast.FindAll({
    param($a)
    $a -is [System.Management.Automation.Language.IfStatementAst] -and
    $a.Extent.Text -like '*Get-Content*' -and (
        $a.Extent.Text -like '*Env:*' -or
        $a.Extent.Text -like '*.env*' -or
        $a.Extent.Text -like '*SetEnvironmentVariable*'
    )
}, $true) | Sort-Object { $_.Extent.Text.Length })
if ($found.Count -ne 1) {
    Write-Error "expected exactly 1 .env-loading if-statement in $rel, got $($found.Count)"
    exit 5
}
Invoke-Expression $found[0].Extent.Text"""


def _function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^function\s+{re.escape(name)}\s*\{{(?P<body>.*?)^\}}\s*$",
        source,
    )
    assert match is not None, f"PowerShell function not found: {name}"
    return match.group("body")


def _env_read_statements(source: str) -> list:
    """挑出「读取 .env」的 Get-Content 语句（容忍反引号/管道续行）。

    判定依据：语句文本内出现 `.env` 或 `$envFile`。据此可漏掉与 .env 无关的
    Get-Content（例如 jax-services.ps1 读 PID 文件的 `Get-Content $f -Raw`）。
    """
    lines = source.splitlines()
    found = []
    for idx, line in enumerate(lines):
        if "Get-Content" not in line:
            continue
        stmt = line
        cursor = idx
        while stmt.rstrip().endswith(("`", "|")) and cursor + 1 < len(lines):
            cursor += 1
            stmt += " " + lines[cursor].strip()
        if ".env" in stmt or "$envFile" in stmt:
            found.append(stmt)
    return found


def _ps_script(channel: _Channel) -> str:
    path = _ROOT / channel.rel
    body = [
        _PS_HEADER,
        f"$rel = '{channel.rel}'",
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$tokens, [ref]$parseErrors)",
        # ${rel} 而非 $rel：双引号内 "$rel:" 会被解析成带作用域的变量引用（语法错误）。
        'if ($parseErrors.Count -gt 0) { Write-Error "parse errors in ${rel}: $($parseErrors.Count)"; exit 4 }',
        f"$Root = '{_ROOT}'",
        *channel.preamble,
    ]
    if channel.mode == "function":
        body.append(f"$name = '{channel.target}'")
        body.append(_PS_FIND_FUNCTION)
    else:
        body.append(_PS_FIND_STATEMENT)
    body.append("@{")
    for name in _PATH_VARS:
        body.append(f"  {name.lower()} = $env:{name}")
    body.append("} | ConvertTo-Json -Compress")
    return "\n".join(body)


def _run_channel(channel: _Channel) -> dict:
    """在真实 PowerShell 中执行脚本里抽出的 env 加载片段，回读 4 条路径变量。"""
    encoded = base64.b64encode(_ps_script(channel).encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        # PS 的 stderr 是 CLIXML 且按 CP936 编码；不加 errors=replace 会让解码线程
        # 抛异常并把 stderr 置为 None，掩盖真正的失败原因。
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"[{channel.rel}] env-load fragment execution failed (exit {proc.returncode}): "
        f"{proc.stderr[:500]}"
    )
    return _parse_flat_json(proc.stdout)


@pytest.mark.parametrize(
    "channel", _CHANNELS, ids=[c.rel.rsplit("/", 1)[-1] for c in _CHANNELS]
)
def test_channel_declares_utf8_decoding(channel: _Channel) -> None:
    """静态腿：每条通道读 .env 的 Get-Content 必须显式 -Encoding UTF8。

    禁止依赖系统 ANSI 默认解码：PS5.1 会把 UTF-8 无 BOM 的 .env 按 GBK 解码，
    mojibake 掉每一个含中文的路径（SSL_CERT_FILE、RTC_BRIDGE_CONTROL_PLANE_*_FILE ...）。
    """
    source = (_ROOT / channel.rel).read_text(encoding="utf-8-sig")
    scope = (
        _function_body(source, channel.target) if channel.mode == "function" else source
    )
    statements = _env_read_statements(scope)
    assert statements, f"{channel.rel}: no .env Get-Content read found"
    assert any(
        re.search(r"-\s*Encoding\s+UTF8", stmt, re.IGNORECASE) for stmt in statements
    ), (
        f"{channel.rel}: .env Get-Content must declare -Encoding UTF8 "
        f"(statement under test: {statements[0].strip()!r}) — on Windows PowerShell 5.1 "
        "the default decodes UTF-8 (no BOM) .env as system ANSI/GBK, mojibaking every "
        "non-ASCII path"
    )


@pytest.mark.parametrize(
    "channel", _CHANNELS, ids=[c.rel.rsplit("/", 1)[-1] for c in _CHANNELS]
)
def test_channel_injects_chinese_paths_verbatim(channel: _Channel) -> None:
    """实测腿：真实执行各通道的 env 加载片段，4 条中文路径逐字注入且文件存在。"""
    values = _run_channel(channel)

    for name in _PATH_VARS:
        value = values.get(name.lower(), "")
        assert value, (
            f"[{channel.rel}] {name} not injected (empty) — the extracted fragment "
            "did not load .env at all (check $Root/$envFile preamble)"
        )
        for marker in _GBK_MOJIBAKE_MARKERS:
            assert marker not in value, (
                f"[{channel.rel}] {name} carries GBK mojibake marker {marker!r}: "
                f"{value!r} — .env decoded as system ANSI/GBK instead of UTF-8"
            )
        assert "监视app" in value, (
            f"[{channel.rel}] {name} is mojibaked (no 监视app substring): {value!r} — "
            ".env decoded as GBK"
        )
        assert Path(value).is_file(), (
            f"[{channel.rel}] {name} points to a nonexistent file: {value!r}"
        )


def _parse_flat_json(stdout: str) -> dict:
    text = stdout.strip()
    # 容忍 PS 输出前导杂散行（如片段里的 Write-Host）：取最后一个以 { 开头的行块。
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].lstrip().startswith("{"):
            return json.loads(" ".join(lines[idx:]))
    raise AssertionError(f"no JSON object in env-load output: {text[:300]!r}")
