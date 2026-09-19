r"""契约：探本机端口的 HTTP 探测点必须显式绕代理。

背景（2026-09-19，claim `windows-popup-free` 的取证工具链）
--------------------------------------------------------
本仓已经踩过这个坑一次，而且**只修了半边**：`docs/OPS-003-live-test.md:98-100` 写着

> 「**代理坑（必踩）**：本机 `HTTP_PROXY/HTTPS_PROXY=127.0.0.1:7890`，`websockets` 默认
> 信任代理导致连不上公网中继（报错为空）。已修：`backend/relay/relay_client.py` 与
> `scripts/mock_phone_client.py` 的 `websockets.connect(..., proxy=None)`。」

**websockets 侧修了，urllib / httpx 侧的 loopback 探测没修。**

实测证据（不是回忆）：

    outputs/2026-09-19-urlopen-proxy-fresh-process-cells.txt
    outputs/2026-09-19-httpx-loopback-proxy-cells.txt

    env HTTP_PROXY=<活代理> 时
      urllib.request.urlopen("http://127.0.0.1:P/health")  -> ERR HTTPError（代理的 502）
      httpx.Client().get("http://127.0.0.1:P/health")      -> HTTP 502（代理的响应）
      两个客户端侧都记录到绝对 URI http://127.0.0.1:P/health  ← 请求真的发给了代理
    build_opener(ProxyHandler({})) / trust_env=False 之后   -> 200，代理侧 0 条

即：**端口是活的，读数却是"服务死了"。**最坏的两处后果是不对称的 ——
`cloudbridge/supervisor.py` 那条会喂 `/status` 的 `rtc_bridge_health`（桥活着却报死），
`scripts/sim/run-sim-e2e.py` 那条会喂门禁的 `bridge_metrics`（失败即字段整体缺失）。

测量陷阱（为什么"自己建 opener"不只是绕代理）
--------------------------------------------
`urllib.request._opener` 是**进程级全局**，代理地址在进程内第一次 `urlopen` 时被冻结：
先让 `getproxies()` 返回空再 urlopen，之后即使设上 `HTTP_PROXY` 也照样直连、代理 0 条
（`cache_from_clean` 格）。所以同一份代码的读数取决于**它在进程生命周期里的位置**。
自己建 opener 顺带把这个不确定性也消掉了 —— 由
`test_urlopen_global_opener_freezes_proxy_at_first_call` 钉住。

本文件的立场
------------
1) **urllib 类（本文件的主要断言）**：只要文件里有 loopback URL 字面量，就不许再出现
   裸 `urlopen(`；必须 `build_opener(ProxyHandler({}))` + `opener.open()`。这一条现在是绿的。
2) **httpx 类**：同病因、已实测，**已修**（`trust_env=False`）。改之前先把
   `trust_env` 到底影响什么枚举清楚（httpx 0.28.1）——它只管两件事：
   ①环境/系统代理（`_client.py:685/1399` `allow_env_proxies = trust_env and transport is None`，
   取 `urllib.request.getproxies()`，Windows 上还会读注册表）；
   ②`SSL_CERT_FILE`/`SSL_CERT_DIR`（`_config.py:34/36`，源码标注 `# pragma: nocover`）。
   本版本**不经** `trust_env` 启用 netrc（`_client.py` 里没有 netrc 引用）。这三处都只打
   本机 loopback、不依赖上面两项 ⇒ 关掉是零副作用的。
3) **websockets 类**：这才是本仓最早踩到这个坑的一族（`docs/OPS-003-live-test.md:98-100`）。
   非测试文件已全部显式 `proxy=None`（`scripts/e2e_verify.py`、`scripts/mock_phone_client.py`、
   `backend/relay/relay_client.py`）；`backend/tests/unit/**` 里还有 20 处**未绕代理**，
   记在 `WS_TEST_DEBT` 里（**那是欠债不是豁免**：修一处就必须从表里删一处）。
   注意 `websockets.connect` 的 `proxy` 默认值是 `True`（= 按环境代理），所以这不是纸面风险。
4) **覆盖完整性**：任何新出现的 loopback 探测点（无论哪个客户端族）都必须先登记，
   否则本文件报红 —— 这才是防复发。
5) **检测器不许恒真**：`test_detector_is_not_vacuous` 用故意不绕代理的样本证明它能红；
   `test_httpx_bypass_reaches_loopback_under_dead_proxy` /`..._websockets_...` 再用
   真起一个本机服务 + 死代理做**行为**验证（带阳性对照）。
6) **复核本锁时不要用 shell `grep` 计数**：本机 MSYS `grep.EXE` 对含 `{}` 的模式会给**假 0**
   （同一文件、同一时刻，`grep -c -F 'ProxyHandler({})'` 有时 1 有时 0，而 Python 字节计数
   稳定为 1）。证据：`outputs/2026-09-19-shell-grep-brace-pattern-false-zero.txt`。
   本文件的一切判定都走 AST + 字节级读数；人工复核也请用 `python -c` 而不是 `grep`。
   本文件自身**不调用任何 `grep`**（唯一的子进程是 `sys.executable -c`）。
7) **本文件自己被排除在扫描面外**，原因写在 `SELF_EXCLUDED_FILES` 上方：行为验证必须
   故意造一个"按环境取代理的客户端"当阳性对照，那正是本文件要禁的东西。
   这个豁免被 `test_self_exclusion_is_narrow` 钉住（精确只许一个文件、不许连坐兄弟文件）。
"""
from __future__ import annotations

import ast
import asyncio
import http.server
import re
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SCAN_ROOTS = ("cloudbridge", "backend", "scripts", "tools")
SKIP_PARTS = {
    "node_modules", "__pycache__", "target", "dist", "build", ".venv", "venv",
    "site-packages", "tmp", "outputs", ".git", "qa-task22-android-buildredirect-nobom-20260810-094500661-655aea80",
}

LOOPBACK_LITERAL = re.compile(r"(?:https?|wss?)://(127\.0\.0\.1|localhost|\[::1\])", re.I)
WS_LOOPBACK_LITERAL = re.compile(r"wss?://(127\.0\.0\.1|localhost|\[::1\])", re.I)

# ---------------------------------------------------------------------------
# 本文件的自豁免：**测量仪器**不在扫描面内
# ---------------------------------------------------------------------------
# 行为验证需要**故意造一个按环境取代理的 httpx 客户端**来当阳性对照 ——
# 那正是本文件要禁的东西。仪器要能造出被测对象，所以仪器自己必须被排除。
# 这种豁免是危险动作（"藏问题的地方"），所以它被两件事钉住：
#   ① 只允许有这一个文件（`test_self_exclusion_is_narrow` 精确相等断言）；
#   ② 该文件里不许出现业务性质的 loopback 探测（只有测试内起的 127.0.0.1 服务）。
SELF_EXCLUDED_FILES = {"backend/tests/contract/test_loopback_probe_proxy_contract.py"}
_SELF_EXCLUDED_ABS = {(REPO_ROOT / p).resolve() for p in SELF_EXCLUDED_FILES}

# ---------------------------------------------------------------------------
# 登记表（改动这里必须同时改代码，否则本文件报红）
# ---------------------------------------------------------------------------
# 已改成显式绕代理的 urllib 点：这些文件里**不许再出现裸 urlopen**
URLLIB_FIXED = {
    "cloudbridge/supervisor.py":
        "产品路径：喂 /status 的 rtc_bridge_health —— 桥活着却报死",
    "scripts/sim/run-sim-e2e.py":
        "喂门禁指标 bridge_metrics —— 失败即字段整体缺失",
    "scripts/o019_rotation_window_e2e.py":
        "o019 轮换窗口 e2e 的就绪探测（写锁时量出来的，与裁定三处同形）",
    "tools/verify_approval_e2e.py":
        "本地 e2e 就绪探测（3 处）",
}

# 已知未修的 httpx 点：**欠债**。现在必须是空的（三处已修，见 HTTPX_FIXED）。
# 留着这个空表是为了让"它必须为空"成为一条**可读的断言**，而不是"没人记得还有这回事"。
HTTPX_DEBT: dict[str, str] = {}

# 已改成显式绕代理的 httpx 点：这些文件里**不许再出现按环境取代理的 httpx 客户端**
HTTPX_FIXED = {
    "scripts/e2e_verify.py":
        "httpx.Client(timeout=15.0, trust_env=False) —— 全部请求都打 BASE=127.0.0.1:8000",
    "scripts/mock_phone_client.py":
        "httpx.AsyncClient(timeout=1.0, trust_env=False) —— 只轮询 /relay/health，client 局部于此函数",
    "scripts/poc_001_model.py":
        "httpx.AsyncClient(timeout=60.0, trust_env=False) —— 只打 BASE=127.0.0.1:19080",
}

# 已改成显式绕代理的 websockets 点（非测试代码）
WS_FIXED = {
    "scripts/e2e_verify.py":
        "websockets.connect(WS_URL, proxy=None) —— WS_URL=ws://127.0.0.1:8000/ws/pet",
    "scripts/mock_phone_client.py":
        "websockets.connect(url, proxy=None) —— url 可能是外部中继，但本仓统一绕代理",
    "backend/relay/relay_client.py":
        "websockets.connect(..., proxy=None) 两处 —— 中继/网关（OPS-003 首次踩坑处）",
}

# **未修**的 websockets loopback 点：只在测试文件里，共 20 处 / 6 文件。
# 值 = 该文件里"url 静态可判为 loopback 且没写 proxy=" 的调用点**数量**：
# 用数量而不是行号，是为了"新增一处"必定报红、而无关改动挪行不误报。
# 这**不是豁免**：修一处就必须把这里的数字减一处，减到空表为止。
# `websockets.connect` 的 `proxy` 默认值是 `True`（= 按环境代理，17.0.1 实测），
# 所以这些点在设了 HTTP_PROXY 的机器上会"服务活着却连不上"。
WS_TEST_DEBT = {
    "backend/tests/unit/test_downlink_frame_trace.py": 2,
    "backend/tests/unit/test_rtc_bridge_ack_report.py": 1,
    "backend/tests/unit/test_rtc_bridge_apm_ack.py": 1,
    "backend/tests/unit/test_rtc_bridge_ctrl_relay.py": 1,
    "backend/tests/unit/test_rtc_bridge_server.py": 6,
    "backend/tests/unit/test_rtc_bridge_session_contract.py": 9,
}

# requests / aiohttp 族：目前一处都没有。出现即必须登记。
OTHER_CLIENT_DEBT: dict[str, str] = {}


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------
def _iter_py_files(root: Path = REPO_ROOT) -> list[Path]:
    out: list[Path] = []
    for root_name in SCAN_ROOTS:
        base = root / root_name
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            if p.resolve() in _SELF_EXCLUDED_ABS:
                continue
            out.append(p)
    return sorted(out)


def _loopback_texts(root: Path = REPO_ROOT) -> dict[str, str]:
    """只返回含 loopback URL 字面量的文件（相对路径 -> 源码）。

    `root` 可换：变异用例把待测文件摆进 tmp 目录的同构树里，用**同一个检测器**判它。
    """
    out: dict[str, str] = {}
    for p in _iter_py_files(root):
        text = p.read_text(encoding="utf-8", errors="replace")
        if LOOPBACK_LITERAL.search(text):
            out[p.relative_to(root).as_posix()] = text
    return out


def _parse(text: str) -> ast.Module:
    return ast.parse(text)


def _urlopen_call_lines(text: str) -> list[int]:
    """裸 `urlopen(...)` / `urllib.request.urlopen(...)` 调用的行号。

    注意 `def fake_urlopen(...)` 这类**定义**不算：这里只认 Call 节点，
    而 `fake_urlopen` 的属性名是 `fake_urlopen` 不是 `urlopen`。
    """
    lines: list[int] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "urlopen":
            lines.append(node.lineno)
        elif isinstance(f, ast.Name) and f.id == "urlopen":
            lines.append(node.lineno)
    return sorted(lines)


def _build_opener_calls(text: str) -> list[tuple[int, bool]]:
    """`build_opener(...)` 调用：(行号, 是否显式传了**空**的 ProxyHandler)。

    这是"裸 urlopen"之外的另一半，而且是更隐蔽的一半：
    `build_opener()` 不传参 ⇒ 默认挂 `ProxyHandler()` ⇒ 它从环境变量/注册表取代理
    ⇒ 一样会劫持 loopback。写成 `opener.open(...)` 看起来很"规范"，但缺陷原样回来了。
    所以 loopback 文件里**每一个** `build_opener` 都必须显式 `ProxyHandler({})`。
    """
    out: list[tuple[int, bool]] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "build_opener"):
            continue
        bypassed = any(_is_empty_proxy_handler(a) for a in node.args)
        out.append((node.lineno, bypassed))
    return sorted(out)


def _is_empty_proxy_handler(node: ast.AST) -> bool:
    """`ProxyHandler({})` / `ProxyHandler(proxies={})` ⇒ 显式空代理（绕代理）。"""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "ProxyHandler"):
        return False
    args = list(node.args)
    for kw in node.keywords:
        if kw.arg == "proxies":
            args.append(kw.value)
    if len(args) != 1:
        return False
    arg = args[0]
    return isinstance(arg, ast.Dict) and not arg.keys and not arg.values


def _httpx_client_constructions(text: str) -> list[tuple[int, bool]]:
    """`httpx.Client(...)` / `httpx.AsyncClient(...)` 构造：(行号, 是否显式绕代理)。"""
    out: list[tuple[int, bool]] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in {"Client", "AsyncClient"}):
            continue
        if not (isinstance(f.value, ast.Name) and f.value.id == "httpx"):
            continue
        if any(kw.arg == "transport" for kw in node.keywords):
            # 自建 transport 的话代理行为由 transport 决定，不看 trust_env
            out.append((node.lineno, True))
            continue
        bypassed = False
        for kw in node.keywords:
            if kw.arg == "trust_env" and isinstance(kw.value, ast.Constant):
                bypassed = kw.value.value is False
            elif kw.arg in {"proxies", "proxy"} and isinstance(kw.value, ast.Constant):
                bypassed = kw.value.value is None
        out.append((node.lineno, bypassed))
    return sorted(out)


def _httpx_client_kwargs(text: str) -> list[tuple[int, str, dict]]:
    """`httpx.Client/AsyncClient(...)` 的 (行号, 类名, 关键字实参字面量)。

    行为测试要**用站点自己那套 kwargs** 去建客户端 —— 这样"站点改了 timeout"不会让
    行为测试失真，而"站点少了 trust_env=False"会被当场发现。
    """
    out: list[tuple[int, str, dict]] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in {"Client", "AsyncClient"}):
            continue
        if not (isinstance(f.value, ast.Name) and f.value.id == "httpx"):
            continue
        kw: dict = {}
        for k in node.keywords:
            if k.arg is None:
                continue
            try:
                kw[k.arg] = ast.literal_eval(k.value)
            except (ValueError, SyntaxError):
                kw[k.arg] = "<非字面量>"
        out.append((node.lineno, f.attr, kw))
    return sorted(out)


def _module_string_consts(text: str) -> dict[str, str]:
    """模块级 `NAME = "字面量"` -> 值。用于解析 `connect(WS_URL)` 这种间接写法。"""
    out: dict[str, str] = {}
    for node in _parse(text).body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _ws_connect_calls(text: str) -> list[tuple[int, bool, bool]]:
    """`websockets.connect(...)`：(行号, url 是否静态判定为 loopback, 是否显式 proxy=None)。

    url 判定顺序：
      1) 调用点源码里直接有 `ws(s)://loopback` 字面量（含 f-string / 间接常量）；
      2) 第一个位置参数是模块级字符串常量，而该常量是 loopback URL（如 `WS_URL`）。
    其余（形参、运行时拼接）一律**不判**为 loopback：静态不可判就不硬判，
    不让检测器去猜 —— 猜出来的红/绿都不值钱。
    """
    consts = _module_string_consts(text)
    out: list[tuple[int, bool, bool]] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (
            isinstance(f, ast.Attribute)
            and f.attr == "connect"
            and isinstance(f.value, ast.Name)
            and f.value.id == "websockets"
        ):
            continue
        is_lb = False
        if node.args:
            a0 = node.args[0]
            seg = ast.get_source_segment(text, a0) or ""
            is_lb = bool(WS_LOOPBACK_LITERAL.search(seg))
            if not is_lb and isinstance(a0, ast.Name) and a0.id in consts:
                is_lb = bool(WS_LOOPBACK_LITERAL.search(consts[a0.id]))
        if not is_lb:
            is_lb = bool(WS_LOOPBACK_LITERAL.search(ast.get_source_segment(text, node) or ""))
        bypassed = False
        for kw in node.keywords:
            if kw.arg == "proxy" and isinstance(kw.value, ast.Constant):
                bypassed = kw.value.value is None
        out.append((node.lineno, is_lb, bypassed))
    return sorted(out)


def _ws_violations(root: Path = REPO_ROOT) -> dict[str, list[int]]:
    """url 静态可判为 loopback、却没写 `proxy=None` 的 websockets 调用点。"""
    out: dict[str, list[int]] = {}
    for rel, text in _loopback_texts(root).items():
        lines = [ln for ln, is_lb, bypassed in _ws_connect_calls(text) if is_lb and not bypassed]
        if lines:
            out[rel] = sorted(lines)
    return out


def _ws_bypassed_files(root: Path = REPO_ROOT) -> set[str]:
    """有 loopback websockets 调用点、且已显式绕代理的文件。"""
    return {
        rel
        for rel, text in _loopback_texts(root).items()
        if any(is_lb and bypassed for _, is_lb, bypassed in _ws_connect_calls(text))
    }


def _httpx_violations(root: Path = REPO_ROOT) -> dict[str, list[int]]:
    return {
        rel: sorted(ln for ln, b in _httpx_client_constructions(text) if not b)
        for rel, text in _loopback_texts(root).items()
        if any(not b for _, b in _httpx_client_constructions(text))
    }


def _httpx_loopback_files(root: Path = REPO_ROOT) -> set[str]:
    return {
        rel for rel, text in _loopback_texts(root).items()
        if _httpx_client_constructions(text)
    }


_OTHER_CLIENT_CALLS = {
    "requests": ({"get", "post", "put", "delete", "head", "request", "Session"}, "requests"),
    "aiohttp": ({"ClientSession"}, "aiohttp"),
}

def _other_client_constructions(text: str) -> list[str]:
    """requests / aiohttp 的客户端构造或请求调用（出现即需登记）。"""
    found: list[str] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            for family, (names, modname) in _OTHER_CLIENT_CALLS.items():
                if f.value.id == modname and f.attr in names:
                    found.append(f"{family}:{f.attr}@{node.lineno}")
    return sorted(found)


# ---------------------------------------------------------------------------
# 1. 主要断言：loopback 文件里不许有裸 urlopen
# ---------------------------------------------------------------------------
def test_no_bare_urlopen_in_loopback_files() -> None:
    """探本机端口却用裸 `urlopen(` ⇒ 会被 `HTTP_PROXY` 劫持，活端口读成死。

    这就是本文件存在的理由。修法：`build_opener(ProxyHandler({}))` + `.open()`
    （与 `scripts/field-evidence/win_popup_capture.py:319` 同构）。
    """
    violations = {
        rel: _urlopen_call_lines(text)
        for rel, text in _loopback_texts().items()
        if _urlopen_call_lines(text)
    }
    assert not violations, (
        "这些文件在探本机端口却用裸 urlopen —— 设了 HTTP_PROXY 时请求会发给代理，"
        f"端口活着也读成死: {violations}\n"
        "改法见 scripts/field-evidence/win_popup_capture.py:319"
    )


def test_loopback_urlopen_sites_are_registered() -> None:
    """覆盖完整性：出现新的 loopback+urllib 点必须先登记，否则报红。"""
    found = {
        rel for rel, text in _loopback_texts().items() if _urlopen_call_lines(text)
    }
    unregistered = found - set(URLLIB_FIXED)
    assert not unregistered, (
        f"这些文件含 loopback URL 字面量且有裸 urlopen，但未登记: {sorted(unregistered)}\n"
        "先修（ProxyHandler({})）再登记进 URLLIB_FIXED。"
    )


def test_no_env_reading_build_opener_in_loopback_files() -> None:
    """另一个半边：loopback 文件里 `build_opener()` 不传参 ⇒ 它照样从环境读代理。

    写成 `opener.open(...)` 看起来很规范，缺陷却原样回来了。所以 loopback 文件里
    **每一个** `build_opener` 都必须显式传空的 `ProxyHandler({})`。
    这一条是设计上面的变异用例时才发现的洞 —— 只查裸 urlopen 时会漏掉它。
    """
    violations = {
        rel: _build_opener_calls(text)
        for rel, text in _loopback_texts().items()
        if any(not bypassed for _, bypassed in _build_opener_calls(text))
    }
    assert not violations, (
        "这些文件的 build_opener 没显式绕代理 —— 不传 ProxyHandler({}) 就是按环境/注册表取代理"
        f": {violations}"
    )


# ---------------------------------------------------------------------------
# 2. httpx：loopback 探测必须显式绕代理（已修）+ 覆盖完整性
# ---------------------------------------------------------------------------
def test_no_httpx_client_reads_env_proxy_in_loopback_files() -> None:
    """loopback 文件里不许有按环境取代理的 httpx 客户端（`trust_env` 默认为 `True`）。

    实测：设了 HTTP_PROXY 时 `httpx.Client().get("http://127.0.0.1:P/…")` 走代理、
    读成 502，代理侧能记录到绝对 URI；加 `trust_env=False` 后 200 且代理侧 0 条。
    依据: outputs/2026-09-19-httpx-loopback-proxy-cells.txt
    """
    violations = _httpx_violations()
    assert not violations, (
        "这些文件的 httpx 客户端会按环境取代理，探本机端口会把活端口读成死: "
        f"{violations}\n"
        "改法：httpx.Client(..., trust_env=False)（httpx 0.28.1 里它只管环境/系统代理与 "
        "SSL_CERT_FILE/DIR，对只打 loopback 的客户端无副作用）"
    )


def test_httpx_loopback_sites_are_registered() -> None:
    """覆盖完整性：出现新的 httpx loopback 客户端文件必须先登记，否则报红。"""
    found = _httpx_loopback_files()
    unregistered = found - set(HTTPX_FIXED)
    assert not unregistered, (
        f"这些文件有 httpx 客户端且含 loopback URL 字面量，但未登记: {sorted(unregistered)}\n"
        "先加 trust_env=False 再登记进 HTTPX_FIXED。"
    )


def test_httpx_debt_register_is_empty() -> None:
    """`HTTPX_DEBT` 现在必须是空的 —— 三处已修，欠债清零。

    这一条防的是"修了却不更新登记表"和"又悄悄长回来"。要往这里放东西，
    必须在同一行写下**为什么修不动**。
    """
    assert HTTPX_DEBT == {}, (
        f"httpx 欠债表非空: {HTTPX_DEBT}\n"
        "要么把它修掉（trust_env=False），要么在注释里写清为什么修不动。"
    )


# ---------------------------------------------------------------------------
# 2b. websockets：本仓最早踩到这个坑的一族
# ---------------------------------------------------------------------------
def test_ws_loopback_sites_bypass_proxy_or_are_registered_debt() -> None:
    """loopback 的 `websockets.connect` 必须显式 `proxy=None`，未修的必须先登记。

    `websockets.connect` 的 `proxy` 默认值是 `True`（= 按环境代理，本机实测 17.0.1），
    所以漏写就等于"设了 HTTP_PROXY 的机器上连不上本机端口"—— 本仓在
    `docs/OPS-003-live-test.md:98-100` 已经踩过一次（那次只修了中继侧）。
    """
    violations = _ws_violations()
    assert set(violations) == set(WS_TEST_DEBT), (
        "websockets loopback 未绕代理清单与登记不符。\n"
        f"  实际: {violations}\n"
        f"  登记: {WS_TEST_DEBT}\n"
        "变长 ⇒ 有新漏点；变短 ⇒ 修了却没减登记。"
    )
    # 关键：欠债**只允许**留在测试文件里。产品/脚本里再出现一处，绝不接受登记了事。
    non_test = sorted(rel for rel in violations if "/tests/" not in f"/{rel}")
    assert not non_test, (
        f"产品/脚本里出现了未绕代理的 loopback websockets 点，不许登记了事，必须修: {non_test}"
    )


def test_ws_test_debt_register_is_exact() -> None:
    """欠债表的**数量**必须与实际处数一致：新增一处就报红，无关改动挪行不误报。"""
    actual = {rel: len(lines) for rel, lines in _ws_violations().items()}
    assert actual == WS_TEST_DEBT, (
        "websockets 测试欠债表与实际处数不符。\n"
        f"  实际: {actual}\n"
        f"  登记: {WS_TEST_DEBT}\n"
        "修一处就要减一处 —— 这张表不是豁免，是待办。"
    )


def test_ws_bypassed_loopback_sites_are_registered() -> None:
    """覆盖完整性：新出现的"已绕代理的 loopback websockets 点"也要登记。"""
    unregistered = _ws_bypassed_files() - set(WS_FIXED)
    assert not unregistered, (
        f"这些文件有已绕代理的 loopback websockets 点，但未登记: {sorted(unregistered)}"
    )


def test_other_http_client_families_are_registered() -> None:
    """requests / aiohttp 族的 loopback 探测点：现在一处都没有，出现即必须登记。"""
    found = {
        rel: _other_client_constructions(text)
        for rel, text in _loopback_texts().items()
        if _other_client_constructions(text)
    }
    assert set(found) == set(OTHER_CLIENT_DEBT), (
        f"出现了未登记的 requests/aiohttp loopback 探测点: {found}"
    )


def test_self_exclusion_is_narrow() -> None:
    """自豁免只许有一个文件，而且不许顺手把兄弟文件一起排除。

    没有这一条，"扫全仓"可以悄悄变成"扫我想扫的"——那才是真正会烂掉的门禁。
    """
    assert SELF_EXCLUDED_FILES == {
        "backend/tests/contract/test_loopback_probe_proxy_contract.py"
    }, "自豁免清单被改宽了 —— 不允许"

    scan = {p.relative_to(REPO_ROOT).as_posix() for p in _iter_py_files()}
    assert SELF_EXCLUDED_FILES.isdisjoint(scan), "自豁免没生效（它仍出现在扫描面里）"
    for sibling in (
        "backend/tests/contract/test_windows_popup_field_evidence_contract.py",
        "backend/tests/contract/test_field_evidence_probe_contract.py",
    ):
        assert sibling in scan, f"豁免范围过宽：{sibling} 被一起排除了"


# ---------------------------------------------------------------------------
# 3. 检测器不许恒真
# ---------------------------------------------------------------------------
def test_detector_is_not_vacuous() -> None:
    """喂故意不绕代理的样本，检测器必须能指出来;喂正确写法必须安静。

    没有这一条，上面那些断言完全可能是"恒真的装饰"。
    """
    bare_urllib = textwrap.dedent("""
        import urllib.request
        URL = "http://127.0.0.1:19093/health"
        urllib.request.urlopen(URL, timeout=2)
    """)
    assert LOOPBACK_LITERAL.search(bare_urllib)
    assert _urlopen_call_lines(bare_urllib) == [4], "裸 urlopen 没被认出来"

    fixed_urllib = textwrap.dedent("""
        import urllib.request
        OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        OPENER.open(URL, timeout=2)
    """)
    assert _urlopen_call_lines(fixed_urllib) == [], "改好的写法被误报"

    # `def fake_urlopen(...)` 是定义不是调用，不算
    spy = "def fake_urlopen(req, timeout=0, context=None):\n    return None\n"
    assert _urlopen_call_lines(spy) == [], "把测试替身的定义误认成裸 urlopen"

    assert LOOPBACK_LITERAL.search('URL = "http://127.0.0.1:19093/health"')
    assert not LOOPBACK_LITERAL.search('URL = "https://api.example.com/health"')
    # 仅有端口号、没有 scheme 的注释不该被当成 URL
    assert not LOOPBACK_LITERAL.search("GET 127.0.0.1:19093/health")

    assert _httpx_client_constructions("import httpx\nhttpx.Client(timeout=1.0)\n") == [(2, False)]
    assert _httpx_client_constructions(
        "import httpx\nhttpx.Client(timeout=1.0, trust_env=False)\n"
    ) == [(2, True)]
    assert _httpx_client_constructions(
        "import httpx\nhttpx.AsyncClient(timeout=1.0)\n"
    ) == [(2, False)]
    assert _other_client_constructions(
        "import requests\nrequests.get('http://127.0.0.1:8000')\n"
    ) == ["requests:get@2"]

    # build_opener 那一支：不传参 = 按环境取代理（违规）；传空 ProxyHandler = 绕代理
    assert _build_opener_calls(
        "import urllib.request\nurllib.request.build_opener()\n"
    ) == [(2, False)]
    assert _build_opener_calls(
        "import urllib.request\n"
        "urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
    ) == [(2, True)]
    assert _build_opener_calls(
        "import urllib.request\n"
        "urllib.request.build_opener(urllib.request.ProxyHandler(proxies={}))\n"
    ) == [(2, True)]
    # 非空的 ProxyHandler 不算绕代理 —— 那只是换了一个代理
    assert _build_opener_calls(
        "import urllib.request\n"
        "urllib.request.build_opener(urllib.request.ProxyHandler({'http': 'http://p:1'}))\n"
    ) == [(2, False)]


# ---------------------------------------------------------------------------
# 3b. 变异：把真实文件里的绕代理去掉，锁必须红
# ---------------------------------------------------------------------------
_MUTATION_FILE = "cloudbridge/supervisor.py"
_MUTATION_BYPASS = "urllib.request.build_opener(urllib.request.ProxyHandler({}))"


def _stage(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _unbypassed_build_openers(root: Path) -> dict[str, list[tuple[int, bool]]]:
    return {
        rel: _build_opener_calls(text)
        for rel, text in _loopback_texts(root).items()
        if any(not bypassed for _, bypassed in _build_opener_calls(text))
    }


def _bare_urlopen_files(root: Path) -> dict[str, list[int]]:
    return {
        rel: _urlopen_call_lines(text)
        for rel, text in _loopback_texts(root).items()
        if _urlopen_call_lines(text)
    }


def test_lock_turns_red_when_a_bypass_is_removed(tmp_path: Path) -> None:
    r"""**变异验证**：把真实文件的 `ProxyHandler({})` 去掉 ⇒ 锁必须红。

    用**真实的 `cloudbridge/supervisor.py`** 做变异（不是手编样本），并且带阳性对照：
    未变异的那一份必须先被判为干净 —— 否则"变红"可能只是因为检测器对什么都红。
    """
    src = (REPO_ROOT / _MUTATION_FILE).read_text(encoding="utf-8")
    assert src.count(_MUTATION_BYPASS) == 1, (
        "变异目标不唯一/找不到 —— 变异未生效，本用例失去意义。"
    )

    # 阳性对照：未变异的真实文件，检测器必须安静
    _stage(tmp_path, _MUTATION_FILE, src)
    assert not _unbypassed_build_openers(tmp_path), (
        "阳性对照失败：未变异的真实文件被判违规（检测器过严）"
    )
    assert not _bare_urlopen_files(tmp_path), (
        "阳性对照失败：未变异的真实文件里还有裸 urlopen"
    )

    # 变异①：去掉绕代理 —— `build_opener(ProxyHandler({}))` -> `build_opener()`
    _stage(tmp_path, _MUTATION_FILE,
           src.replace(_MUTATION_BYPASS, "urllib.request.build_opener()"))
    assert _MUTATION_FILE in _unbypassed_build_openers(tmp_path), (
        "去掉 ProxyHandler({}) 之后锁竟然还是绿的 —— 这个锁没有牙齿"
    )

    # 变异②：换成裸 urlopen —— 另一半检测也必须红
    _stage(tmp_path, _MUTATION_FILE,
           src.replace("LOOPBACK_OPENER.open(", "urllib.request.urlopen("))
    assert _MUTATION_FILE in _bare_urlopen_files(tmp_path), (
        "换成裸 urlopen 之后锁竟然还是绿的 —— 裸 urlopen 那一半检测没有牙齿"
    )


def test_lock_turns_red_when_httpx_or_ws_bypass_is_removed(tmp_path: Path) -> None:
    r"""变异：把**真实文件**里新加的 `trust_env=False` / `proxy=None` 去掉 ⇒ 锁必须红。

    和 `ProxyHandler({})` 那条同构，但对象换成 httpx 与 websockets 两族 ——
    新加的两族如果只有"读源码"的断言而没有被变异检验过，就不知道它们有没有牙齿。
    同样带阳性对照：未变异的真实文件必须先被判干净。
    """
    httpx_rel = "scripts/e2e_verify.py"
    ws_rel = "scripts/e2e_verify.py"

    httpx_src = (REPO_ROOT / httpx_rel).read_text(encoding="utf-8")
    assert httpx_src.count("trust_env=False") == 1, "变异目标不唯一/找不到 —— 变异未生效"
    assert httpx_src.count("websockets.connect(WS_URL, proxy=None)") == 1, (
        "变异目标不唯一/找不到 —— 变异未生效"
    )

    # 阳性对照：未变异的真实文件，两族检测都必须安静
    _stage(tmp_path, httpx_rel, httpx_src)
    assert not _httpx_violations(tmp_path), "阳性对照失败：未变异的真实文件被判 httpx 违规"
    assert not _ws_violations(tmp_path), "阳性对照失败：未变异的真实文件被判 ws 违规"

    # 变异③：去掉 httpx 的绕代理 ⇒ httpx 那一族必须红
    _stage(tmp_path, httpx_rel, httpx_src.replace(", trust_env=False", ""))
    assert httpx_rel in _httpx_violations(tmp_path), (
        "去掉 trust_env=False 之后锁竟然还是绿的 —— httpx 那一族检测没有牙齿"
    )

    # 变异④：去掉 websockets 的绕代理 ⇒ ws 那一族必须红
    _stage(tmp_path, ws_rel, httpx_src.replace("websockets.connect(WS_URL, proxy=None)",
                                               "websockets.connect(WS_URL)"))
    assert ws_rel in _ws_violations(tmp_path), (
        "去掉 proxy=None 之后锁竟然还是绿的 —— websockets 那一族检测没有牙齿"
    )


# ---------------------------------------------------------------------------
# 4. 测量陷阱：_opener 在进程内第一次 urlopen 时冻结代理
# ---------------------------------------------------------------------------
_CHILD_PREAMBLE = textwrap.dedent("""
    import http.server, os, socket, sys, threading, urllib.request

    s = socket.socket(); s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]; s.close()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2")
            self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    URL = "http://127.0.0.1:%d/health" % PORT

    def probe():
        try:
            with urllib.request.urlopen(URL, timeout=3) as r:
                return "HTTP %d" % r.status
        except Exception as e:
            return "ERR " + type(e).__name__
""")


def _run_child(body: str) -> str:
    r = subprocess.run([sys.executable, "-c", _CHILD_PREAMBLE + textwrap.dedent(body)],
                       capture_output=True, text=True, timeout=60)
    return (r.stdout or "").strip() + (r.stderr or "").strip()


def test_urlopen_global_opener_freezes_proxy_at_first_call() -> None:
    r"""钉住测量陷阱：`urllib.request._opener` 是进程级全局，代理地址在**第一次 urlopen** 时冻结。

    同环境、同 URL，两次调用的结果取决于"第一次调用时进程里有没有代理"。
    所以自己建 opener 不只是绕代理，也让读数不再依赖进程历史。

    第 2 格是第一格的**阳性对照**：证明那个死代理真的会让请求失败 ——
    没有它，第 1 格的"仍直连"可能只是死代理根本没用上。
    """
    # 格 1：第一次调用时无代理（打桩 getproxies 建立前提），之后设上死代理 ⇒ 应当仍直连
    frozen = _run_child("""
        import urllib.request
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            os.environ.pop(k, None)
        urllib.request.getproxies = lambda: {}
        first = probe()
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"   # 死代理
        second = probe()
        print("FIRST=" + first)
        print("SECOND=" + second)
    """)
    assert "FIRST=HTTP 200" in frozen, f"前提不成立（首次应当直连成功）:\n{frozen}"
    assert "SECOND=HTTP 200" in frozen, (
        "设上 HTTP_PROXY 之后本该被冻结的代理映射却生效了 —— "
        f"这条陷阱的形态变了，请重新量:\n{frozen}"
    )

    # 格 2（阳性对照）：进程一开始就带死代理 ⇒ 必须失败
    blocked = _run_child("""
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            os.environ.pop(k, None)
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        print("ONLY=" + probe())
    """)
    assert "ONLY=ERR" in blocked, (
        f"阳性对照失败：死代理居然没让请求失败，格 1 的结论就不可信:\n{blocked}"
    )


# ---------------------------------------------------------------------------
# 5. 行为验证：带着死代理也必须能探通本机端口（每处一带阳性对照）
# ---------------------------------------------------------------------------
def _dead_proxy() -> tuple[str, socket.socket]:
    """死代理地址：绑一个端口但**不 listen** ⇒ 连接必被拒绝，且不会被别的进程抢走。

    比写死 `http://127.0.0.1:9` 稳：端口由内核分配、必然空闲，只要这个 socket
    不关就不会被占用。多出来的好处是——如果哪台机器真的有人在 9 号端口监听，
    写死端口的那种测试会"阳性对照静默失效"，这里不会。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return f"http://127.0.0.1:{s.getsockname()[1]}", s


def _set_dead_proxy_env(monkeypatch, url: str) -> None:
    """把代理环境变量设成死代理，并**清掉 NO_PROXY** —— 否则绕行可能来自 no_proxy 而不是代码。"""
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "WS_PROXY", "ws_proxy"):
        monkeypatch.setenv(var, url)


def _start_hit_server() -> tuple[str, list[int], object]:
    """本机 HTTP 服务 -> (url, hits, shutdown)。hits[0] 就是收到的请求数（代理劫持的判据）。"""
    hits = [0]

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            hits[0] += 1
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: A003
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/health", hits, srv.shutdown


def _httpx_probe(cls_name: str, kwargs: dict, url: str) -> str:
    try:
        if cls_name == "AsyncClient":

            async def go() -> str:
                async with httpx.AsyncClient(**kwargs) as c:
                    r = await c.get(url)
                    return f"HTTP {r.status_code}"

            return asyncio.run(go())
        with httpx.Client(**kwargs) as c:
            r = c.get(url)
            return f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return "ERR " + type(e).__name__


@pytest.mark.parametrize("rel", sorted(HTTPX_FIXED))
def test_httpx_bypass_reaches_loopback_under_dead_proxy(rel: str, monkeypatch) -> None:
    r"""行为验证（不读源码也算数）：带着死代理仍能探通本机端口。

    用的是**站点自己写的那套 kwargs** 建客户端，所以它同时钉住两件事：
    站点确实写了 `trust_env=False`，而且这一行确实是那个让读数正确的原因。

    格 1（正）：站点 kwargs（含 `trust_env=False`）⇒ 200，且本机服务命中 1 次。
    格 2（阳性对照）：同一份 kwargs 只把 `trust_env` 换成 `True`
        ⇒ 必须失败，且本机服务**命中 0 次**（证明请求真的被交给了代理）。
    没有格 2，格 1 的"探通"可能只是死代理根本没生效 —— 这正是本文件反复在防的假绿。
    """
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    sites = _httpx_client_kwargs(text)
    assert sites, f"{rel} 里找不到 httpx 客户端构造 —— 本用例会变成恒真的装饰"

    dead, keep = _dead_proxy()
    try:
        _set_dead_proxy_env(monkeypatch, dead)
        url, hits, shutdown = _start_hit_server()
        try:
            for lineno, cls_name, kw in sites:
                assert kw.get("trust_env") is False, (
                    f"{rel}:{lineno} 的 httpx.{cls_name}(...) 没写 trust_env=False"
                )

                hits[0] = 0
                got = _httpx_probe(cls_name, dict(kw), url)
                assert got == "HTTP 200", (
                    f"{rel}:{lineno} 带死代理时没探通本机端口: {got}"
                )
                assert hits[0] == 1, (
                    f"{rel}:{lineno} 没打到本机服务（命中 {hits[0]}）"
                )

                hits[0] = 0
                bad = dict(kw)
                bad["trust_env"] = True
                got_bad = _httpx_probe(cls_name, bad, url)
                assert got_bad.startswith("ERR"), (
                    f"阳性对照失败：{rel}:{lineno} 去掉 trust_env=False 后竟然也能探通"
                    f"（{got_bad}）—— 死代理没生效，格 1 的结论不可信"
                )
                assert hits[0] == 0, (
                    f"阳性对照失败：{rel}:{lineno} 去掉绕代理后请求仍打到了本机服务"
                    f"（命中 {hits[0]}）"
                )
        finally:
            shutdown()
    finally:
        keep.close()


def test_websockets_default_uses_env_proxy() -> None:
    """钉住这条事实：`websockets.connect` 的 `proxy` 默认值是 `True`（= 按环境代理）。

    这条不是装饰：`WS_TEST_DEBT` 里那 20 处之所以算**欠债**而不是洁癖，全赖这个默认值。
    哪天它变了，欠债表的性质也得跟着改 —— 所以让它报红提醒。
    `websockets/proxy.py:103-118` 会先问 `urllib.request.proxy_bypass`，再取
    `urllib.request.getproxies()` 里的 `ws`/`socks`/`https`/`http` 项。
    """
    import websockets

    sig = __import__("inspect").signature(websockets.connect)
    assert sig.parameters["proxy"].default is True, (
        "websockets.connect 的 proxy 默认值不再是 True —— WS_TEST_DEBT 的前提变了，重新量"
    )


def _start_ws_server() -> tuple[int, list[int]]:
    """本机 ws 服务（独立线程 + 独立事件循环）-> (port, hits)。"""
    from websockets.asyncio.server import serve

    hits = [0]
    state: dict = {}
    ready = threading.Event()

    async def handler(ws) -> None:
        hits[0] += 1
        await ws.send("hi")
        await ws.wait_closed()

    def run() -> None:
        async def main() -> None:
            async with serve(handler, "127.0.0.1", 0) as server:
                state["port"] = server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()

        asyncio.run(main())

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(10), "本机 ws 服务没起来"
    return state["port"], hits


def _ws_probe(url: str, proxy_kwargs: dict) -> str:
    import websockets

    async def go() -> str:
        async with websockets.connect(url, open_timeout=3, **proxy_kwargs) as ws:
            return "OK " + await ws.recv()

    try:
        return asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        return "ERR " + type(e).__name__


def test_websockets_bypass_reaches_loopback_under_dead_proxy(monkeypatch) -> None:
    r"""`proxy=None` 才是绕代理（行为验证，两格同进程）。

    格 1（正）：`proxy=None` + 死代理 ⇒ 连上本机 ws，服务侧命中 1。
    格 2（阳性对照）：同环境但不写 `proxy`（默认 `True`）⇒ 必须失败、服务侧命中 0。
    """
    dead, keep = _dead_proxy()
    try:
        _set_dead_proxy_env(monkeypatch, dead)
        port, hits = _start_ws_server()
        url = f"ws://127.0.0.1:{port}/ws"

        hits[0] = 0
        got = _ws_probe(url, {"proxy": None})
        assert got == "OK hi", f"proxy=None 时没连上本机 ws: {got}"
        assert hits[0] == 1, f"本机 ws 服务命中数不对（应为 1）: {hits[0]}"

        hits[0] = 0
        got_bad = _ws_probe(url, {})
        assert got_bad.startswith("ERR"), (
            f"阳性对照失败：不写 proxy（默认 True）居然也连上了（{got_bad}）—— 死代理没生效"
        )
        assert hits[0] == 0, (
            f"阳性对照失败：请求仍打到了本机 ws 服务（命中 {hits[0]}）"
        )
    finally:
        keep.close()
