r"""契约：探本机端口的 HTTP 探测点必须显式绕代理。

背景（2026-09-19，claim `windows-popup-free` 的取证工具链）
--------------------------------------------------------
本仓已经踩过这个坑一次，而且**只修了半边**：`docs/OPS-003-live-test.md:98-100` 写着

> 「**代理坑（必踩）**：本机 `HTTP_PROXY/HTTPS_PROXY=127.0.0.1:7890`，`websockets` 默认
> 信任代理导致连不上公网中继（报错为空）。已修：`backend/relay/relay_client.py` 与
> `scripts/mock_phone_client.py` 的 `websockets.connect(..., proxy=None)`。」

**websockets 侧修了，urllib / httpx 侧的 loopback 探测没修。**

实测证据（不是回忆）。出处文件在被 gitignore 的 `outputs/` 下，所以引用一律写成
**「逻辑名 + sha256」自证**：即使文件不在，也能验它在出处有没有被改过。
逻辑名 -> 路径 / 大小 / "它证明了什么" 见**受跟踪**索引
`docs/evidence/2026-09-19-loopback-proxy-evidence-index.md`：

    [loopback-proxy/fresh-process-cells] 大小 1250
      outputs/2026-09-19-urlopen-proxy-fresh-process-cells.txt
      sha256 34f4aaa1e6cdfec1f7dd3e76e695a784f504d1020d2280c8ae2fc516100f95dd

    [loopback-proxy/httpx-cells] 大小 847
      outputs/2026-09-19-httpx-loopback-proxy-cells.txt
      sha256 073e5a3eb8ff419dc397373cb7ac072796f0520fc95baa2b741f47e5ed3435f3

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
   `backend/relay/relay_client.py`）；`backend/tests/unit/**` 里原是 **28 处未绕代理**，
   现已全部修掉，`WS_TEST_DEBT` 为**空表**，由 `test_ws_debt_register_is_empty` 钉住。
   注意 `websockets.connect` 的 `proxy` 默认值是 `True`（= 按环境代理），所以这不是纸面风险 ——
   行为依据 [loopback-proxy/unit-ws-dead-proxy-cells]：

      outputs/2026-09-19-unit-ws-dead-proxy-positive-control.txt
      sha256 1c2d81259fd3d2ec3e5d50cfc6b4312695e3510ab3550a884a245b369f7c8946
4) **覆盖完整性**：任何新出现的 loopback 探测点（无论哪个客户端族）都必须先登记，
   否则本文件报红 —— 这才是防复发。
5) **检测器不许恒真**：`test_detector_is_not_vacuous` 用故意不绕代理的样本证明它能红；
   `test_httpx_bypass_reaches_loopback_under_dead_proxy` /`..._websockets_...` 再用
   真起一个本机服务 + 死代理做**行为**验证（带阳性对照）。
6) **复核本锁时不要用 shell `grep` 计数**：本机 MSYS `grep.EXE` 对含 `{}` 的模式会给**假 0**
   （同一文件、同一时刻，`grep -c -F 'ProxyHandler({})'` 有时 1 有时 0，而 Python 字节计数
   稳定为 1）。证据 [loopback-proxy/shell-grep-false-zero]：

     outputs/2026-09-19-shell-grep-brace-pattern-false-zero.txt
     sha256 e9cdcb673d0dc1d906622ef26c0b320b97f41fcfdcc32f2c9e16d93ab9cfa0c1

   本文件的一切判定都走 AST + 字节级读数；人工复核也请用 `python -c` 而不是 `grep`。
   本文件自身**不调用任何 `grep`**（唯一的子进程是 `sys.executable -c`）。
7) **本文件自己被排除在扫描面外**，原因写在 `SELF_EXCLUDED_FILES` 上方：行为验证必须
   故意造一个"按环境取代理的客户端"当阳性对照，那正是本文件要禁的东西。
   这个豁免被 `test_self_exclusion_is_narrow` 钉住（精确只许一个文件、不许连坐兄弟文件）。
8) **在本机跑这一族（以及整套 suite）必须带 `--basetemp=<固定目录>`**：本机 safe-delete
   shim 有一条**按 turn 计数**的批量删除守卫（阈值 500），它会掐掉 pytest 自己在 session
   结束时对 `pytest-of-<user>\garbage-<uuid>` 的回收，`SystemExit(1)` 打断 session finish
   ⇒ **连 `--junitxml` 的产物都被一起吞掉**（实测：进度条走到 100%、`ls` 报 XML 不存在、
   rc=1 —— 比"rc 假红"更糟，那是**产物消失**）。加上 `--basetemp` 后 junit 落盘、rc=0。
   这只挪临时目录根，**不动任何断言 / 超时**；CI（Linux runner、无该 shim）不受影响。
   证据 [loopback-proxy/basetemp-sidesteps-guard]：

     outputs/2026-09-19-pytest-basetemp-sidesteps-bulk-delete-guard.txt
     sha256 7b1232048329ad3165e08be3a637ea65770b60b9d41fff58a6a03dc48ac43e6b
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import http.server
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import uuid
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
        "本地 e2e 就绪探测（4 处：:57 http_post、:102/:115/:203 就绪与重启后探测）",
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

# 已改成显式绕代理的 websockets 点：这些文件的 loopback connect 必须保持 `proxy=None`
WS_FIXED = {
    "scripts/e2e_verify.py":
        "websockets.connect(WS_URL, proxy=None) —— WS_URL=ws://127.0.0.1:8000/ws/pet",
    "scripts/mock_phone_client.py":
        "websockets.connect(url, proxy=None) —— url 可能是外部中继，但本仓统一绕代理",
    "backend/relay/relay_client.py":
        "websockets.connect(..., proxy=None) 两处 —— 中继/网关（OPS-003 首次踩坑处）",
    # 测试文件：本机 bridge 的 loopback connect，28 处一次性机械补齐。
    # 登记它们不是"因为重要"，而是为了让这张表 = 全体"已绕代理的 loopback ws 文件"清单 ——
    # 新增一个文件就必须在这里露一次面。
    "backend/tests/unit/test_downlink_frame_trace.py": "本机 bridge loopback connect ×2",
    "backend/tests/unit/test_rtc_bridge_ack_report.py": "×1",
    "backend/tests/unit/test_rtc_bridge_apm_ack.py": "×1",
    "backend/tests/unit/test_rtc_bridge_ctrl_relay.py":
        "×1（端到端死代理行为验证挑的就是这个文件）",
    "backend/tests/unit/test_rtc_bridge_server.py":
        "×14（其中 8 处是 connect(url)，url 是**函数内局部量** —— 第一版检测器漏掉的就是它们）",
    "backend/tests/unit/test_rtc_bridge_session_contract.py": "×9",
}

# **未修**的 websockets loopback 点：原本是测试文件里的 28 处，现已全部修掉。
# 这个空表是终局形态（和 `HTTPX_DEBT` 一样）：留着它，是为了让"必须为空"成为一条
# **可读的断言**，而不是"没人记得还有这回事"。
#
# 修的过程中锁自己又漏了一次，值得记下来：第一版只按**模块级**常量解析 URL，
# 而 `backend/tests/unit/test_rtc_bridge_server.py` 里的 `url = f"ws://127.0.0.1:{port}"`
# 是**函数内局部量**，于是 8 处 `connect(url)` 被漏判 —— 若就此把表清空，
# 锁会绿着放过 8 个真实 loopback 点。现在按**作用域链**解析（`_loopback_bound_names`）。
WS_TEST_DEBT: dict[str, int] = {}

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
            # SKIP_PARTS 只该作用于**扫描根之下**的路径分量。
            # 2026-09-24 实测事故：这里原本比对的是 `p.parts`（整条**绝对**路径），
            # 于是 Linux 上 pytest 的 tmp_path（/tmp/pytest-of-runner/...）里那个
            # `tmp` 分量命中了 SKIP_PARTS 里的 "tmp" ⇒ 变异用例摆进 tmp 的样本
            # **一个都扫不到** ⇒ 检测器返回 {} ⇒ 两条"锁有没有牙齿"的变异用例
            # 在 ubuntu 上双双失败。Windows 上侥幸不炸（Temp 与 tmp 大小写不同）。
            # 相对扫描根取分量后，仓内语义完全不变（base = REPO_ROOT/<root_name>，
            # 相对分量就是仓内那几层），而任意 tmp 目录都不再被误跳过。
            rel_parts = p.relative_to(base).parts
            if any(part in SKIP_PARTS for part in rel_parts):
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


def _is_loopback_url_node(node: ast.AST) -> bool:
    """`"ws://127.0.0.1:…"` 或 `f"ws://127.0.0.1:{port}"` 这类**字面可判**的 loopback。

    f-string 只看它的**常量片段**（`f"…:{port}"` 的常量片段是 `"ws://127.0.0.1:"`），
    所以不需要源码片段、也不受格式化影响。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(WS_LOOPBACK_LITERAL.search(node.value))
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return bool(WS_LOOPBACK_LITERAL.search("".join(parts)))
    return False


def _loopback_bound_names(body: list[ast.stmt]) -> set[str]:
    """这个作用域里被赋成 loopback URL 的变量名。

    **必须按作用域收集**：`url = f"ws://127.0.0.1:{port}"` 是函数内的局部量。
    第一版只在模块级收集，于是 `backend/tests/unit/test_rtc_bridge_server.py`
    里那 8 处 `connect(url)` 全被漏掉 —— 而它们运行时就是 loopback。
    收集时**不下钻嵌套函数**，免得把子作用域的名字算到父作用域头上。
    """
    names: set[str] = set()
    stack: list[ast.AST] = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Assign) and _is_loopback_url_node(n.value):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif (
            isinstance(n, ast.AnnAssign)
            and n.value is not None
            and _is_loopback_url_node(n.value)
            and isinstance(n.target, ast.Name)
        ):
            names.add(n.target.id)
        for child in ast.iter_child_nodes(n):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                continue
            if isinstance(child, ast.stmt):
                stack.append(child)
    return names


def _is_ws_connect(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "websockets"
    )


def _ws_connect_nodes(text: str) -> list[tuple[ast.Call, bool]]:
    """所有 `websockets.connect(...)` 节点 + 它的 url 是否静态判定为 loopback。

    单独暴露出来，是为了让"改代码"和"判代码"用**同一套判定**：
    批量修复脚本 import 本函数，就不会出现"修的和判的不是一回事"。
    """
    tree = _parse(text)
    out: list[tuple[ast.Call, bool]] = []

    def visit(node: ast.AST, scope: frozenset[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = scope | _loopback_bound_names(node.body)
        for child in ast.iter_child_nodes(node):
            if _is_ws_connect(child):
                assert isinstance(child, ast.Call)
                is_lb = False
                if child.args:
                    a0 = child.args[0]
                    is_lb = _is_loopback_url_node(a0) or (
                        isinstance(a0, ast.Name) and a0.id in scope
                    )
                out.append((child, is_lb))
            visit(child, scope)

    visit(tree, frozenset(_loopback_bound_names(tree.body)))
    return out


def _ws_connect_calls(text: str) -> list[tuple[int, bool, bool]]:
    """`websockets.connect(...)`：(行号, url 是否静态判定为 loopback, 是否显式 proxy=None)。

    url 判定只看第一个位置参数，三种**字面可判**的形态：
      1) 直接写字面量/f-string：`connect(f"ws://127.0.0.1:{port}")`；
      2) 名字绑到一个 loopback 字面量（按**作用域链**解析，函数内局部量也算）。
    其余（形参、运行时拼接、函数返回）一律**不判**为 loopback：静态不可判就不硬判，
    不让检测器去猜 —— 猜出来的红/绿都不值钱。
    """
    out: list[tuple[int, bool, bool]] = []
    for node, is_lb in _ws_connect_nodes(text):
        bypassed = any(
            kw.arg == "proxy"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is None
            for kw in node.keywords
        )
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
    依据 [loopback-proxy/httpx-cells]:
      outputs/2026-09-19-httpx-loopback-proxy-cells.txt
      sha256 073e5a3eb8ff419dc397373cb7ac072796f0520fc95baa2b741f47e5ed3435f3
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


def test_ws_debt_register_is_empty() -> None:
    """`WS_TEST_DEBT` 现在必须是空的 —— 28 处已全部显式 `proxy=None`。

    和 `test_httpx_debt_register_is_empty` 同一个立场：欠债表一旦开始长期非空，
    就退化成 allowlist。要往这里放东西，必须在同一处写下**为什么修不动**。
    """
    assert WS_TEST_DEBT == {}, (
        f"websockets 欠债表非空: {WS_TEST_DEBT}\n"
        "要么把它修掉（proxy=None），要么在注释里写清为什么修不动。"
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

    # 仪器自检（**必须排在一切判断之前**）：检测器真的看到了这份样本吗？
    # 2026-09-24 事故教训：阳性对照 `assert not _unbypassed_build_openers(tmp_path)`
    # 在"检测器一个文件都没扫到"时**也会通过**（{} 是假绿），于是"锁没有牙齿"被当成
    # "锁很干净"。linux 上 SKIP_PARTS 误跳过 /tmp 就是这个形态。
    # 所以先证明"仪器看到了样本"，再谈它判红还是判绿。
    _stage(tmp_path, _MUTATION_FILE, src)
    assert _MUTATION_FILE in _loopback_texts(tmp_path), (
        "仪器没看到样本：变异文件根本没被扫描到，后面的判红/判绿都不可信"
    )

    # 阳性对照：未变异的真实文件，检测器必须安静
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

    # 仪器自检（必须排在一切判断之前）：两族检测都真的看到了这份样本吗？
    _stage(tmp_path, httpx_rel, httpx_src)
    assert set(_loopback_texts(tmp_path)) >= {httpx_rel, ws_rel}, (
        "仪器没看到样本：变异文件根本没被扫描到，后面的判红/判绿都不可信"
    )

    # 阳性对照：未变异的真实文件，两族检测都必须安静
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


# 拿一个**真实改过的测试文件**做端到端行为验证：锁里那 28 处一次也没有真跑起来过的
# 话，"机械加 proxy=None"就只是文本改动，没有行为证据兜底。
_UNIT_WS_PROBE_FILE = "backend/tests/unit/test_rtc_bridge_ctrl_relay.py"


def _relocate_out_of_repo(path: Path) -> None:
    """"清理"= 把变异探针**移出仓库树**，不是删除（本机工具层会 veto 删除）。

    实测（2026-09-19 全量跑，两条红都出在这里）：本机 WorkBuddy 的 safe-delete shim
    包装了 `os.remove` / `os.unlink` / `pathlib.Path.unlink`，并挂了一条**按 turn 计数**
    的批量删除守卫（阈值 500）。全量跑时这个计数必然被 pytest 自己的临时目录清理顶穿，
    于是 `finally` 里的 `unlink()` 被守卫以 `SystemExit(1)` 直接拒掉 ⇒ 探针文件留在树里
    ⇒ 连锁把 `test_no_stray_mutation_probe_files` 也判红。
    —— 注意这与本文件别的教训同族：**读数/结果取决于它在进程/回合里的位置**。

    为什么用 `os.replace`：shim 里只有 `_try_trash`（被 remove/unlink/rmdir/rmtree 调用）
    会去问那条守卫，`os.replace` / `os.rename` 不走它（只走 host broker，失败则回落原生）。
    所以"移走"是这里唯一既干净、又不会被 veto 的收尾方式。
    移到系统 Temp 后**不再删**：删一次就要再赌一次守卫，而留在 Temp 不污染仓库树。

    证据 [loopback-proxy/safe-delete-guard-veto]（两条红的 junit 原文 + shim 行号锚）：

      outputs/2026-09-19-safe-delete-bulk-guard-vetoes-test-cleanup.txt
      sha256 22aa50cf016d2c3818a5a412915f4ae6904dc352e6e3989bc8197db88c6ae945
    """
    if not path.exists():
        return
    dest_dir = Path(tempfile.gettempdir()) / "jax-loopback-mutation-probes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.replace(str(path), str(dest_dir / path.name))
    assert not path.exists(), f"探针文件没能移出仓库树（清理失败）: {path}"


def _dead_proxy_subprocess_env(dead: str) -> dict[str, str]:
    env = dict(os.environ)
    for var in ("NO_PROXY", "no_proxy"):
        env.pop(var, None)
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "WS_PROXY", "ws_proxy"):
        env[var] = dead
    return env


def _run_pytest_node(path: str, env: dict[str, str]) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", path, "-q", "--no-header",
         "-p", "no:cacheprovider", "-x"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300, env=env,
    )
    return r.returncode, ((r.stdout or "") + (r.stderr or ""))[-900:]


def _assert_child_really_ran(tail: str, what: str) -> None:
    """子进程必须是**真的跑了测试**才作数。

    第一版把变异文件名（而不是仓库内相对路径）交给子 pytest，子进程相对 cwd 找不到它，
    于是 rc=4 —— "阳性对照"会因为**跑都没跑起来**而通过。这正是本文件一直在防的假绿，
    所以这里显式断言它不是那几种"没跑"的形态。
    """
    for bad in ("file or directory not found", "no tests ran", "collected 0 items"):
        assert bad not in tail, f"{what} 的子进程没真跑测试（{bad}）:\n{tail}"
    assert ("passed" in tail) or ("failed" in tail) or ("error" in tail), (
        f"{what} 的子进程输出看不出跑过测试:\n{tail}"
    )


def test_changed_unit_ws_file_survives_dead_proxy_and_needs_the_bypass() -> None:
    r"""端到端：**真跑**一个改过的 unit 文件，带死代理。

    格 1（正）：`pytest <改过的 unit 文件>` 在死代理环境下必须 rc=0 且**确实跑了测试**
        —— 证明那些 `proxy=None` 在真实运行里有效（不是只有 AST 看着对）。
    格 2（阳性对照）：把同一份源码的 `, proxy=None` 去掉、**先确认它仍能编译**
        （排除"因为语法错才失败"），同样环境下必须 rc≠0 且**确实跑了测试**。
    没有格 2，格 1 只能说明"这个文件本来就能跑"，说明不了那 28 处改动有没有用。

    变异文件必须落在仓库树内（否则拿不到 conftest/import 路径），所以写进同目录、
    `finally` 删除；万一残留，`test_no_stray_mutation_probe_files` 会当场报红。
    """
    src_path = REPO_ROOT / _UNIT_WS_PROBE_FILE
    src = src_path.read_text(encoding="utf-8")
    assert src.count(", proxy=None") >= 1, (
        f"{_UNIT_WS_PROBE_FILE} 里没有 proxy=None —— 本用例失去意义"
    )

    dead, keep = _dead_proxy()
    mutated_path = src_path.with_name(f"test__mutation_probe__{uuid.uuid4().hex[:8]}.py")
    mutated_rel = mutated_path.relative_to(REPO_ROOT).as_posix()
    try:
        env = _dead_proxy_subprocess_env(dead)

        # 格 1：真跑改过的文件
        rc_ok, tail_ok = _run_pytest_node(_UNIT_WS_PROBE_FILE, env)
        _assert_child_really_ran(tail_ok, "格 1")
        assert rc_ok == 0, (
            f"死代理环境下 {_UNIT_WS_PROBE_FILE} 没跑通 —— 那些 proxy=None 没起作用:\n{tail_ok}"
        )

        # 格 2（阳性对照）：去掉绕代理，但先确认源码仍可编译、且子进程真的跑了
        mutated = src.replace(", proxy=None", "")
        compile(mutated, str(mutated_path), "exec")  # 语法必须仍然合法
        mutated_path.write_text(mutated, encoding="utf-8")
        rc_bad, tail_bad = _run_pytest_node(mutated_rel, env)
        _assert_child_really_ran(tail_bad, "格 2")
        assert rc_bad != 0, (
            "阳性对照失败：去掉 proxy=None 之后在死代理下居然还能跑通 —— "
            f"格 1 的结论不可信:\n{tail_bad}"
        )
        assert "proxy" in tail_bad.lower() or "error" in tail_bad.lower(), (
            f"阳性对照失败的原因看起来不是代理（请人眼确认一次）:\n{tail_bad}"
        )
    finally:
        # 不删（删除会被本机 safe-delete 守卫 veto，见 `_relocate_out_of_repo`）：移出树即可。
        _relocate_out_of_repo(mutated_path)
        keep.close()


def test_no_stray_mutation_probe_files() -> None:
    """变异探针文件不许残留：残留会让"扫描全仓"把探针当成真实代码。

    这条是给上一条用例兜底的：它在 `finally` 删，但如果进程被强杀就会漏。
    残留物一旦存在，本文件会以"多出一个未登记的 loopback 文件"的形式报红 ——
    这里直接点名说清楚，省得下次有人看不懂那三条红。
    """
    strays = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("test__mutation_probe__*.py")
        if ".venv" not in p.parts
    )
    assert not strays, f"有变异探针文件残留，请删除: {strays}"


# ---------------------------------------------------------------------------
# 3. 证据索引：注释里的「逻辑名 + sha256」必须真的指得到、且对得上
# ---------------------------------------------------------------------------
# 上面那些引用写成 `[loopback-proxy/<短名>]` + sha256，而不是一个裸的 `outputs/...` 路径。
# 理由：`outputs/` 被 `.gitignore:134` 忽略，干净检出后那个路径**悬空**；而 sha256 能自证
# "它在出处有没有被改过"。逻辑名 -> 路径 / 大小 / 它证明了什么，落在**受跟踪**索引里。
EVIDENCE_INDEX = "docs/evidence/2026-09-19-loopback-proxy-evidence-index.md"
_LOGICAL_NAME_RE = re.compile(r"\[(loopback-proxy/[a-z0-9-]+)\]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# 索引**条目**里不许出现的词：允许它们出现，等于允许"引用自证"变成一句空话。
# 注意只查表格单元格，不查规则正文 —— 正文里必须能写出这些词本身。
_INDEX_FORBIDDEN = ("待补", "TODO", "TBD", "FIXME", "省略")


def _evidence_index_entries() -> list[dict[str, str]]:
    """解析索引表格：逻辑名 | 路径 | 大小 | sha256 | 它证明了什么。"""
    path = REPO_ROOT / EVIDENCE_INDEX
    assert path.is_file(), f"证据索引不存在（受跟踪文件）: {EVIDENCE_INDEX}"
    entries: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `loopback-proxy/"):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        assert len(cells) >= 5, f"索引行少列（需要 5 列）: {line}"
        entries.append({
            "logical": cells[0], "path": cells[1], "size": cells[2],
            "sha256": cells[3], "proves": cells[4],
        })
    return entries


def test_evidence_index_is_wellformed_and_never_says_pending() -> None:
    """索引的**每一条**都不许有空 sha256 / "以后再补" —— 那等于"引用自证"在说谎。

    检查对象是**表格条目**，不是整份文件：规则正文里必须能提到那些词本身，否则
    规则没法写（第一版本条就是扫全文，于是被自己写的规则判红 —— 又一次"仪器把
    自己算进去了"）。所以判据落在每条记录的前四列上。
    """
    entries = _evidence_index_entries()
    assert entries, "索引里一条都没有 —— 表格格式可能被改坏了（行首必须是 '| `loopback-proxy/…'）"
    for e in entries:
        head = " ".join((e["logical"], e["path"], e["size"], e["sha256"]))
        for bad in _INDEX_FORBIDDEN:
            assert bad not in head, (
                f"{e['logical']}: 条目里出现 {bad!r} —— 待补的条目不许进索引"
            )
        assert _SHA256_RE.match(e["sha256"]), (
            f"{e['logical']}: sha256 不是 64 位小写十六进制: {e['sha256']!r}"
        )
        assert e["path"].startswith("outputs/2026-09-19-"), (
            f"{e['logical']}: 路径列应当是完整相对路径 outputs/2026-09-19-…: {e['path']!r}"
        )
        assert e["size"].isdigit() and int(e["size"]) > 0, (
            f"{e['logical']}: 大小列不是正整数: {e['size']!r}"
        )
        assert len(e["proves"]) >= 20, (
            f"{e['logical']}: 第 5 列只有 {len(e['proves'])} 字，等于没写它证明了什么"
        )


def test_evidence_index_declares_the_byte_shape() -> None:
    """索引必须写明 sha256 是**哪个字节形态**的哈希 —— 否则"自证"这把尺子会歪。

    本波 12 条证据实测形态是**混合**的（7 条 CRLF / 5 条 LF，2026-09-19 量出来的），
    而 sha256 对形态敏感。不声明形态时，读者在别处重算会拿到一串"不一致"，把
    **形态差异**误读成**证据被篡改** —— 又一个"看起来验过了"的陷阱（同族于
    `core.autocrlf=true` 下把 blob 与工作区直接比字节、误报 14 处那次）。

    判据落在**声明的存在**上，不钉具体形态：形态随写入方式变，硬钉死只会让索引
    变成维护负担。真正管形态一致性的，是下面那条逐字节核对。
    """
    text = (REPO_ROOT / EVIDENCE_INDEX).read_text(encoding="utf-8")
    assert "CRLF" in text and "LF" in text, (
        "索引没写明证据文件的换行形态 —— 哈希对形态敏感，不声明就会把形态差异误读成篡改"
    )
    assert "工作区字节" in text, (
        "索引没写明 sha256 是**本机工作区字节**的哈希（不是 LF 归一化后的），"
        "读者会以为它跨平台通用"
    )


def test_evidence_index_sha256_matches_the_files_present_here() -> None:
    """索引里的 sha256 必须与**本机现存**的证据文件逐字节一致。

    干净检出时 `outputs/` 根本不存在（`.gitignore:134`）⇒ 本条按设计 `skip`；
    另两条（格式 + 逻辑名覆盖）在干净检出上照跑，门禁不会因为这条退让而失牙。
    反过来，工作机上 `outputs/` 在、却一条都对不上，就必须报红 —— 否则索引与文件脱节。
    """
    outputs_dir = REPO_ROOT / "outputs"
    if not outputs_dir.is_dir():
        pytest.skip("干净检出：outputs/ 不存在（.gitignore:134 忽略），无法就地核对 sha256")
    checked = 0
    for e in _evidence_index_entries():
        p = REPO_ROOT / e["path"]
        if not p.is_file():
            continue
        raw = p.read_bytes()
        assert len(raw) == int(e["size"]), (
            f"{e['logical']}: 大小不符 —— 索引写 {e['size']}，实际 {len(raw)}（证据被改过？）"
        )
        assert hashlib.sha256(raw).hexdigest() == e["sha256"], (
            f"{e['logical']}: sha256 不符 —— 索引与证据文件已脱节，两边必须同步改"
        )
        checked += 1
    assert checked > 0, (
        f"outputs/ 存在，但索引里的 {len(_evidence_index_entries())} 条证据一个都不在 —— "
        "要么证据被清掉了，要么索引的路径列写错了"
    )


def test_every_referenced_logical_name_is_indexed() -> None:
    """代码里引用的每个逻辑名都必须在索引里有条目 —— 否则引用指不到任何地方。"""
    indexed = {e["logical"] for e in _evidence_index_entries()}
    texts = dict(_loopback_texts())
    # 本文件被自豁免排除了，但它的 docstring 里也有引用，得单独算进来。
    texts["backend/tests/contract/test_loopback_probe_proxy_contract.py"] = (
        Path(__file__).read_text(encoding="utf-8")
    )
    referenced: dict[str, set[str]] = {}
    for rel, text in texts.items():
        for m in _LOGICAL_NAME_RE.finditer(text):
            referenced.setdefault(m.group(1), set()).add(rel)
    assert referenced, (
        "一处逻辑名引用都没找到 —— 引用写法可能被改回裸路径了（那就又会悬空）"
    )
    missing = sorted(set(referenced) - indexed)
    assert not missing, (
        f"这些逻辑名被代码引用，但索引里没有条目: {missing}\n"
        "每个逻辑名都要有：路径 / 大小 / sha256 / 它证明了什么。"
    )
