"""jax-voice-bridge 的 9200 HTTP 面契约：真起 socket、真发请求、锁死部署门禁读的那些键。

为什么需要这个文件（既有测试覆盖的是别的层次）
----------------------------------------------
`test_cloudbridge_service_contract.py` 看起来覆盖了这块，但逐条读下来是三层错位：

- `:82-85` 对 `EXPOSE 9200` 只是**文本断言**：`assert "EXPOSE 9200" in text`。
  文本对不等于端口真的在服务。
- `:213` / `:233` / `:549` / `:686` 对 `status()` 是**函数级**断言（直接调方法），
  而且键判据是 `set(payload) >= {...}` / `key in payload` —— 只证明"这些键在"，
  **多一个键、少一个不提的键都不会红**，请求也从没经过 `_Handler`。
- 全仓**没有任何测试对 `/health`、`/healthz`、`/api/v1/voice/bridge/status` 真的发过
  一次 HTTP 请求**。

而这三条路由是部署门禁的输入。`.github/workflows/deploy-cloudrun.yml:475`：

    payload = wait_json("/api/v1/voice/bridge/status", lambda d: d.get("ok") is True)

随后它读 `rtc_bridge.alive` / `sidecar.alive` / `rtc_bridge_health` /
`trtc_sdk_version`（`:478-489`）。也就是说「门禁绿」此前建立在一个**从未被端到端
请求过的 HTTP 面**上：路由写错、状态码映射写反、`Content-Length` 算错导致门禁读不到
完整 body，CI 都会绿。

本文件怎么测
------------
用 `ThreadingHTTPServer` 在 `127.0.0.1` 的**临时端口**（port 0）起**真实的**
`supervisor._Handler`，装配方式与 `main():664-668` 完全一致（把 supervisor 挂成
`_Handler` 的类属性），然后用 stdlib `http.client` 真发请求、真收字节。

不依赖容器 / Electron / TRTC / 真机 / 网络：真实 `BridgeSupervisor()` 的 `__init__`
是**惰性**的（只读环境变量并构造 `Child` 描述对象，不 spawn、不写盘），
`Child.alive()` 在 `proc is None` 时返回 False，因此"什么都没启动"这个状态本身就是
确定性的。

关于 ASCII 结果标记（为什么本文件**不**提供）
--------------------------------------------
本仓在 `scripts/pe-subsystem-verify.py` 上确立了
`PE_SUBSYSTEM=<PASS|FAIL|REPORT_ONLY|INVALID_INPUT>` 这套范式，那是因为**那个脚本
自己就是门禁**。这里门禁是 pytest：结论已经由 `--junitxml` 给出，再往 stdout 打一个
`BRIDGE_HTTP=PASS` 会多出**第二个更弱的裁决通道**，而且它可能在后续用例失败之前就
先打印 PASS。同一份事实有两个可能互相矛盾的出口，正是本仓反复吃的亏。
若确实要一个给部署侧消费的标记，应当另写一个只做三次探测、退出码语义化的 stdlib
脚本 —— 但那时它是**另一个门禁**，不是本文件的补充。
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import logging
import re
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudrun.yml"

# `/health`、`/healthz` 的响应体：**精确**这两个键，多一个都不许。
HEALTH_BODY_KEYS = frozenset({"status"})

# `/api/v1/voice/bridge/status` 的顶层键（supervisor.status() 的返回值形状）。
# 这份清单引用的依据是 `supervisor.py:605-630`；`ok` 是门禁的判定键。
STATUS_TOP_LEVEL_KEYS = frozenset({
    "service",
    "uptime_s",
    "sidecar_enabled",
    "rtc_bridge",
    "sidecar",
    "rtc_bridge_health",
    "sign_url",
    "device_id",
    "trtc_sdk_version",
    "tls_material",
    "audio",
    "ignored_env",
    "ok",
})

# Child.describe() 的形状（supervisor.py:289-302）。门禁读 rtc_bridge.alive /
# sidecar.alive，所以这两个子对象的结构也是门禁输入的一部分。
CHILD_DESCRIBE_KEYS = frozenset({
    "alive", "pid", "starts", "exit_code", "output_tail", "events", "last_join",
})

UNKNOWN_PATH_BODY = {"code": 40400, "message": "not found"}


def _load_supervisor():
    """按路径加载 cloudbridge/supervisor.py（与既有契约测试同一惯用法）。

    不用 `import supervisor`：那会把一个泛用名塞进 sys.path，且会与其它测试
    加载到的同名模块互相覆盖。`sys.modules[spec.name] = module` 必须在 exec 之前，
    否则模块内的 dataclass / 类型转发会失败。
    """
    spec = importlib.util.spec_from_file_location(
        "jax_voice_bridge_supervisor_http", CLOUDBRIDGE / "supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _StubChild:
    """只提供 `_Handler` 需要的 `alive()`。"""

    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def alive(self) -> bool:
        return self._alive


class _StubSupervisor:
    """把状态码映射的两个分支都变成可控输入。

    `/health` 与 `/healthz` 的状态码是 `bridge.alive()` 的函数，`/status` 的状态码是
    `payload["ok"]` 的函数 —— 不注入这两个状态就只能测到"当前恰好是哪个分支"。
    """

    def __init__(self, *, alive: bool, status_ok: bool) -> None:
        self.bridge = _StubChild(alive)
        self._status_payload = {
            "service": "jax-voice-bridge",
            "ok": status_ok,
            "rtc_bridge": {"alive": alive},
            "sidecar": {"alive": alive},
            "rtc_bridge_health": "ok" if alive else "URLError",
            "trtc_sdk_version": "12.7.706",
            "marker": "stub-passthrough",
            # 刻意放一个**非 ASCII** 值：`_send` 用 `ensure_ascii=False` 编码
            # （supervisor.py:640），于是 UTF-8 字节数 > 字符数。
            # 没有它，"Content-Length 按字符数算"这个失败模式在今天的真实 payload 上
            # 根本不可达（三个路由的现值全是 ASCII）—— 那条断言就会变成空转。
            "note": "音频子进程未就绪",
        }

    def status(self) -> dict:
        return dict(self._status_payload)


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    """发一次真实请求，返回 (状态码, 响应头, 原始字节)。

    刻意返回**原始字节**而不只返回解析结果：`Content-Length` 与实际字节数是否一致
    是门禁能不能读全 body 的关键，只比对 `response.json()` 会把这一类错误吃掉。
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


@contextlib.contextmanager
def _served(module, supervisor):
    """在临时端口上起真实 `_Handler`，结束后干净关闭。

    装配方式与 `main():664-668` 一致：把 supervisor 挂成**类属性**
    （`_Handler.supervisor = supervisor`）。

    `create=True` 是必须的，不是保险：`_Handler.supervisor` 在
    `supervisor.py:637` 只是**裸注解**（`supervisor: BridgeSupervisor`），
    注解不产生类属性 —— 于是 `main()` 里那句赋值是**唯一**的装配点。
    少了它，`do_GET` 会在运行期 `AttributeError`，而在此之前没有任何测试能发现。
    patch 结束时该属性会被删除（因为它本来就不存在），不会泄漏给同模块其它用例。
    """
    import unittest.mock

    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module._Handler)
    port = server.server_address[1]
    # poll_interval 调小只为缩短每个用例的关闭等待（默认 0.5s × 每个用例），
    # 不影响任何被断言的字节。
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    # 请求日志会按 handler 的 log_message 走 logger.info；这里调高阈值只为让
    # junitxml / CI 输出干净，不影响任何被断言的字节。
    module.logger.setLevel(logging.WARNING)
    with unittest.mock.patch.object(module._Handler, "supervisor", supervisor, create=True):
        thread.start()
        try:
            yield port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    assert not thread.is_alive(), "HTTP 服务线程未在 5s 内退出"


@pytest.fixture()
def supervisor_module():
    return _load_supervisor()


# --- 1. /health 与 /healthz -------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_returns_200_with_exactly_status_ok_when_bridge_alive(
    supervisor_module, path: str
) -> None:
    """bridge 存活 → 200，且响应体**恰好**是 {"status": "ok"}。

    用 `set(payload) == HEALTH_BODY_KEYS` 而不是 `payload["status"] == "ok"`：
    后者会放过"响应体里被多塞了内部字段"这种改动，而健康探针的响应体是平台侧的
    输入，多塞字段属于对外契约变更。
    """
    module = supervisor_module
    supervisor = _StubSupervisor(alive=True, status_ok=True)
    with _served(module, supervisor) as port:
        status, headers, body = _get(port, path)

        assert status == 200, body
        payload = json.loads(body)
        assert set(payload) == HEALTH_BODY_KEYS, payload
        assert payload == {"status": "ok"}
        assert headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_returns_503_with_exactly_status_unavailable_when_bridge_down(
    supervisor_module, path: str
) -> None:
    """bridge 不存活 → 503，且**仍是 JSON**、键集合不变。

    失败分支比成功分支更容易被写坏：503 若返回 HTML/空体，平台探针会把"没就绪"
    误读成"服务不存在"，两种故障的处置完全不同。
    """
    module = supervisor_module
    supervisor = _StubSupervisor(alive=False, status_ok=False)
    with _served(module, supervisor) as port:
        status, _headers, body = _get(port, path)

        assert status == 503, body
        payload = json.loads(body)
        assert set(payload) == HEALTH_BODY_KEYS, payload
        assert payload == {"status": "unavailable"}


# --- 2. /api/v1/voice/bridge/status（部署门禁的输入） -------------------------


def test_status_route_returns_200_and_passes_the_payload_through(
    supervisor_module
) -> None:
    """`ok=True` → 200，且 handler **不改写** payload（原样透传）。

    门禁读的是 `status()` 的返回值本身。若 handler 在中间做了包装或改名，
    函数级测试（直接调 `status()`）永远发现不了 —— 那正是既有测试的盲区。
    """
    module = supervisor_module
    supervisor = _StubSupervisor(alive=True, status_ok=True)
    with _served(module, supervisor) as port:
        status, headers, body = _get(port, "/api/v1/voice/bridge/status")

        assert status == 200, body
        payload = json.loads(body)
        assert payload == supervisor.status(), "handler 对 payload 做了改写"
        assert headers["content-type"].startswith("application/json")


def test_status_route_returns_503_but_still_sends_the_full_payload(
    supervisor_module
) -> None:
    """`ok=False` → 503，但**响应体仍是完整 payload**。

    门禁在 `:479-481` 把整个 payload 打进日志当死因（`json.dumps(payload)[:600]`）。
    如果 503 只回一个错误壳，运维拿到的是"未就绪"而拿不到"为什么未就绪" ——
    这正是本项目「no silent anything」要防的那类静默。
    """
    module = supervisor_module
    supervisor = _StubSupervisor(alive=False, status_ok=False)
    with _served(module, supervisor) as port:
        status, _headers, body = _get(port, "/api/v1/voice/bridge/status")

        assert status == 503, body
        payload = json.loads(body)
        assert payload == supervisor.status()
        assert set(payload) >= {"ok", "rtc_bridge_health"}


def test_status_route_serves_the_real_supervisor_payload_with_the_exact_key_set(
    supervisor_module
) -> None:
    """**用真实 `BridgeSupervisor`**，锁死门禁输入的形状（精确相等）。

    这是本文件里唯一能挡住"`status()` 加/删/改键"的用例：前面几条用的是 stub，
    而 stub 的形状是我自己写的，它证明不了产品代码的形状。

    真实构造是安全的：`__init__` 只读环境变量、构造 `Child` 描述对象，不 spawn
    子进程、不落盘；两个子进程都未启动 ⇒ `alive()` 为 False ⇒ `ok` 为 False
    ⇒ 路由返回 503，但**payload 是产品自己算出来的那一个**。

    `bridge_health_url` 指向一个必然拒绝连接的 loopback 端口，避免任何真实探测。
    """
    module = supervisor_module
    supervisor = module.BridgeSupervisor()
    supervisor.bridge_health_url = "http://127.0.0.1:1/health"

    with _served(module, supervisor) as port:
        status, _headers, body = _get(port, "/api/v1/voice/bridge/status")

        assert status == 503, body
        payload = json.loads(body)

        assert set(payload) == STATUS_TOP_LEVEL_KEYS, (
            "顶层键集合变了：这是部署门禁的输入。"
            f" 多={sorted(set(payload) - STATUS_TOP_LEVEL_KEYS)}"
            f" 少={sorted(STATUS_TOP_LEVEL_KEYS - set(payload))}"
        )
        for child in ("rtc_bridge", "sidecar"):
            assert set(payload[child]) == CHILD_DESCRIBE_KEYS, (
                f"{child} 的形状变了（门禁读 {child}.alive）: {sorted(payload[child])}"
            )
            assert payload[child]["alive"] is False, "未启动的子进程不得报 alive"

        assert payload["ok"] is False, "两个子进程都未启动时 ok 必须为 False"
        assert payload["service"] == "jax-voice-bridge"


# --- 3. 404 回退 -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/nope",
        "/health/",  # 尾斜杠不是同一条路由
        "/api/v1/voice/bridge/status/extra",
        "/api/v1/voice/bridge/status?query=1",  # 带查询串不等于带路径
    ],
)
def test_unknown_path_returns_404_with_the_stable_error_envelope(
    supervisor_module, path: str
) -> None:
    """未命中路由必须 404 + 固定错误壳，不允许 200 空体或 HTML 错误页。"""
    module = supervisor_module
    supervisor = _StubSupervisor(alive=True, status_ok=True)
    with _served(module, supervisor) as port:
        status, headers, body = _get(port, path)

        assert status == 404, body
        assert json.loads(body) == UNKNOWN_PATH_BODY
        assert headers["content-type"].startswith("application/json")


# --- 4. 传输层：Content-Length 与字节数必须一致 -------------------------------


@pytest.mark.parametrize(
    "path",
    ["/health", "/healthz", "/api/v1/voice/bridge/status", "/nope"],
)
def test_content_length_matches_the_actual_byte_count(supervisor_module, path: str) -> None:
    """`Content-Length` 必须等于实际字节数。

    handler 是手写 `Content-Length` 的（`supervisor.py:643`）。一旦它和实际字节数
    不符（比如按字符数而非 UTF-8 字节数算），客户端要么截断、要么等到超时 ——
    门禁的 `wait_json` 会因此超时，而错误信息只显示"未就绪"，指向完全错误的方向。
    用 `ensure_ascii=False` 编码的中文/非 ASCII 内容最容易踩这个坑。
    """
    module = supervisor_module
    supervisor = _StubSupervisor(alive=True, status_ok=True)
    with _served(module, supervisor) as port:
        _status, headers, body = _get(port, path)

        declared = int(headers["content-length"])
        assert declared == len(body), (
            f"{path}: Content-Length={declared} 与实际字节数 {len(body)} 不一致"
        )
        assert headers["content-type"] == "application/json; charset=utf-8"


def test_the_utf8_byte_count_probe_is_not_vacuous() -> None:
    """自校：stub 的 payload 必须含非 ASCII。

    否则"字节数 == 字符数"，上一条用例里的断言对「Content-Length 按字符数算」
    这个失败模式**恒真** —— 一条永远不会红的断言比没有断言更危险。
    这条用例守住那个前提本身，谁把 stub 改回纯 ASCII，它会先红。
    """
    payload = _StubSupervisor(alive=True, status_ok=True).status()
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text.encode("utf-8")) > len(text), (
        "stub payload 全是 ASCII ⇒ 字节数等于字符数 ⇒ Content-Length 那条用例无法变红"
    )


# --- 5. 门禁与 HTTP 面两端对齐 -------------------------------------------------


_GATE_BLOCK = re.compile(
    r'elif gate == "bridge":(?P<body>.*?)(?=\n\s*else:|\Z)', re.DOTALL
)
_TOP_KEY_CALLS = re.compile(r'(?:data|payload|d)\.get\("([a-z_]+)"')
_CHILD_TUPLE = re.compile(r'for child in \(([^)]*)\)')


def _gate_keys_from(text: str) -> tuple[set[str], list[str]]:
    """从 deploy workflow 的**文本**里提取 bridge 门禁读取的键。

    提取不到就 assert 失败，绝不返回空集：空集会让下游断言全部空转通过，
    而那正是本仓库反复出现的假绿形态（"没提要求 ⇒ 通过"）。
    """
    block = _GATE_BLOCK.search(text)
    assert block is not None, "deploy-cloudrun.yml 里找不到 bridge 门禁块（结构变了）"
    gate = block.group("body")

    top_keys = set(_TOP_KEY_CALLS.findall(gate))
    child_match = _CHILD_TUPLE.search(gate)
    assert child_match is not None, f"门禁不再用 (rtc_bridge, sidecar) 元组遍历: {gate[:200]}"
    children = [c.strip().strip('"') for c in child_match.group(1).split(",") if c.strip()]

    assert len(top_keys) >= 3, f"门禁键提取失败（只拿到 {sorted(top_keys)}），正则需更新"
    assert set(children) >= {"rtc_bridge", "sidecar"}, children

    return top_keys, children


def _assert_gate_keys_served(top_keys: set[str], children: list[str], payload: dict) -> None:
    missing = sorted(k for k in top_keys if k not in payload)
    assert not missing, f"门禁读了 9200 面不提供的顶层键: {missing}（门禁: {sorted(top_keys)}）"
    for child in children:
        assert child in payload, f"门禁读取 {child}，但 /status 不提供该键"
        assert "alive" in payload[child], f"门禁读 {child}.alive，但该子对象没有 alive"


def _real_status_payload(module) -> dict:
    """用真实 supervisor 通过真实 HTTP 取一次 `/status` 的 payload。"""
    supervisor = module.BridgeSupervisor()
    supervisor.bridge_health_url = "http://127.0.0.1:1/health"
    with _served(module, supervisor) as port:
        _status, _headers, body = _get(port, "/api/v1/voice/bridge/status")
    return json.loads(body)


def test_the_deploy_gate_only_reads_keys_this_surface_actually_serves(
    supervisor_module
) -> None:
    """把门禁**源码**读出来，逐键核对 HTTP 面是否真的提供。

    这条测试连接两端：门禁加了新的读取键而 9200 面没提供 → 门禁会在真实部署时
    拿到 None 并静默走错分支（或直接 `sys.exit`），而单元测试全绿。这里把它提前。
    """
    top_keys, children = _gate_keys_from(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    _assert_gate_keys_served(top_keys, children, _real_status_payload(supervisor_module))


def test_gate_coupling_bites_when_the_gate_starts_reading_a_new_key(
    supervisor_module
) -> None:
    """**变异检验**：把门禁改成读一个 9200 面不存在的键，判据必须变红。

    刻意**不**去临时改 `.github/workflows/deploy-cloudrun.yml`：那是另一条工作流正在
    使用的受跟踪文件，为验证一句断言就临时改它再恢复，风险与收益不对等。
    把"门禁文本"当成**纯输入**喂给同一段判据，判据是否承重就已证明 ——
    这也正是我把提取与断言拆成两个纯函数的原因。
    """
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace(
        'data.get("trtc_sdk_version")', 'data.get("brand_new_gate_key")', 1
    )
    assert mutated != text, "变异没生效：workflow 里找不到预期的门禁读取语句"

    top_keys, children = _gate_keys_from(mutated)
    assert "brand_new_gate_key" in top_keys, "变异后的键没被提取到，说明提取逻辑跟不上"

    with pytest.raises(AssertionError, match="brand_new_gate_key"):
        _assert_gate_keys_served(top_keys, children, _real_status_payload(supervisor_module))
