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
2) **httpx 类**：同病因、已实测，但**尚未修**（等 team-lead 裁定，因为修法形状不同：
   `trust_env=False` 会一并关掉其它 trust_env 行为，不是纯等价替换）。这里只做一份
   **精确的欠债登记**：不许悄悄变长，也不许悄悄变短。
3) **覆盖完整性**：任何新出现的 loopback 探测点（无论哪个客户端族）都必须先登记，
   否则本文件报红 —— 这才是防复发。
4) **检测器不许恒真**：`test_detector_is_not_vacuous` 用故意不绕代理的样本证明它能红。
5) **复核本锁时不要用 shell `grep` 计数**：本机 MSYS `grep.EXE` 对含 `{}` 的模式会给**假 0**
   （同一文件、同一时刻，`grep -c -F 'ProxyHandler({})'` 有时 1 有时 0，而 Python 字节计数
   稳定为 1）。证据：`outputs/2026-09-19-shell-grep-brace-pattern-false-zero.txt`。
   本文件的一切判定都走 AST + 字节级读数；人工复核也请用 `python -c` 而不是 `grep`。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SCAN_ROOTS = ("cloudbridge", "backend", "scripts", "tools")
SKIP_PARTS = {
    "node_modules", "__pycache__", "target", "dist", "build", ".venv", "venv",
    "site-packages", "tmp", "outputs", ".git", "qa-task22-android-buildredirect-nobom-20260810-094500661-655aea80",
}

LOOPBACK_LITERAL = re.compile(r"https?://(127\.0\.0\.1|localhost|\[::1\])", re.I)

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

# 已知未修的 httpx 点：**欠债**，等裁定。内容与数量都必须精确。
HTTPX_DEBT = {
    "scripts/e2e_verify.py":
        "httpx.Client(timeout=15.0) 探 http://127.0.0.1:8000 —— 未加 trust_env=False",
    "scripts/mock_phone_client.py":
        "httpx.AsyncClient(timeout=1.0) 探 http://127.0.0.1:{port}/relay/health —— 未加 trust_env=False",
    "scripts/poc_001_model.py":
        "httpx.AsyncClient(timeout=60.0) 探 http://127.0.0.1:19080 —— 未加 trust_env=False",
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
# 2. httpx 欠债登记：精确、响亮、不许悄悄变
# ---------------------------------------------------------------------------
def test_httpx_loopback_debt_register_is_exact() -> None:
    """httpx 侧的 loopback 探测**已实测同病因但尚未修**（等裁定）。

    本用例不断言"它是好的"——它只保证这份欠债清单是**精确的**：
    变长（新增未修的点）报红，变短（有人修了却不更新登记）也报红。
    """
    found = {
        rel for rel, text in _loopback_texts().items()
        if any(not bypassed for _, bypassed in _httpx_client_constructions(text))
    }
    detail = {
        rel: [line for line, bypassed in _httpx_client_constructions(text)
              if not bypassed]
        for rel, text in _loopback_texts().items()
        if _httpx_client_constructions(text)
    }
    assert found == set(HTTPX_DEBT), (
        "httpx loopback 欠债登记与实际不符。\n"
        f"  实际未绕代理: {sorted(found)}  行号明细: {detail}\n"
        f"  登记表:      {sorted(HTTPX_DEBT)}\n"
        "清单变长 ⇒ 有新的未修点；变短 ⇒ 修了却没更新登记表。两种都要改这里。\n"
        "实测依据: outputs/2026-09-19-httpx-loopback-proxy-cells.txt"
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
