"""契约：**产品运行期里没有手机模拟器**（2026-09-17 结构性移除）。

背景
----
线上 `jax-voice-bridge` 仍设着 `BRIDGE_SIM_PHONE=true`，于是商用生产容器里多起了一个
**假手机**：xvfb + 第二个 Electron（`--role=phone`），它走控制面 provisioning、以真实设备
身份进同一个 TRTC 房间，与真实 sidecar 抢**唯一**的会话位。那是把测试装置塞进了生产容器。

修法不是翻 flag、也不是加个脚本，而是**把那条执行路径整个删掉**：启动路径、`/status` 的
`simulation` 段、以及那批 `SIM_*` 环境变量的读取全部不存在了。本文件把"它不在了"钉死，
免得下一次"重构"又把它带回来。

为什么静态断言要用 **AST** 而不是文本匹配
----------------------------------------
`supervisor.py` 里现在**故意**留着解释性注释（`BRIDGE_SIM_PHONE` / `sim_phone` /
`simulation` 这些词都出现在注释里，说明"这里曾经有什么、为什么删了"）。裸子串断言会被
自己的注释误伤，于是反过来逼人删掉注释——那是把文档赶走、把缺陷留下。AST 看的是**代码
构造**（import、函数名、属性、字符串常量），注释天然不参与，因此既精确又不必牺牲注释。

模块为什么不删
--------------
`cloudbridge/sim_phone.py` / `sim_provision.py` 仍由**容器外**的本地 harness
`scripts/sim/run-phone.py`（以及 `scripts/sim/measure-rate-repeat.py`）使用，那是我们在
容器之外唯一的端到端验证手段。出问题的从来不是模块，是**容器在跑它们**。所以本文件最后
两条断言反过来保护它们：谁想"顺手清理"，这里会先红。
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"
SUPERVISOR_SRC = CLOUDBRIDGE / "supervisor.py"

sys.path.insert(0, str(CLOUDBRIDGE))


def _load_supervisor():
    spec = importlib.util.spec_from_file_location("jax_voice_bridge_supervisor_removed",
                                                  SUPERVISOR_SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 1. 静态：执行路径不存在（看 AST 构造，不看注释）-------------------------

_BANNED_IMPORTS = ("sim_phone", "sim_provision")
_BANNED_FUNCS = ("_start_sim_phone", "_reap_sim_phone", "_sim_lines",
                 "_sim_log_dirs", "_sim_log_files")
_BANNED_ATTRS = ("sim_enabled", "sim_phone", "sim_metrics", "sim_device_id",
                 "sim_log_dir", "sim_hold_s", "sim_prompt_text", "sim_prompt_wav",
                 "sim_out_wav", "sim_join_grace_s", "sim_device_name",
                 "sim_owner_credential", "sim_device_credential", "_sim_lock",
                 "_sim_device_id")
# 这三个字符串常量就是「第二个 Electron」「sim 子进程」「对外的 simulation 承诺」的
# 直接标识：只可能来自被移除的那条路径。
_BANNED_STRINGS = ("--role=phone", "sim-phone", "simulation")


def _sim_launch_path_violations(source: str) -> list[str]:
    """源码里残留的模拟器执行路径（空列表 = 干净）。

    为什么用 AST：`supervisor.py` 现在**故意**在注释里保留 `BRIDGE_SIM_PHONE` /
    `sim_phone` / `simulation` 这些词来解释"这里曾经有什么、为什么删了"。裸子串断言会被
    自己的注释误伤，反过来逼人删注释 —— 那是把文档赶走、把缺陷留下。AST 只看 import、
    函数名、属性与字符串常量，注释天然不参与。
    """
    tree = ast.parse(source)
    out: list[str] = []

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    out += [f"import {name}（这就是「容器会跑模拟器」的入口）"
            for name in _BANNED_IMPORTS if name in imported]

    defs = {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out += [f"函数 {name}()（模拟器执行路径的一部分）"
            for name in _BANNED_FUNCS if name in defs]

    attrs = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "self"}
    out += [f"self.{name}（模拟器状态）" for name in _BANNED_ATTRS if name in attrs]

    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    out += [f"字符串常量 {name!r}" for name in _BANNED_STRINGS if name in literals]
    return out


def test_supervisor_has_no_sim_phone_launch_path() -> None:
    """整套 sim 执行路径——线程启动点、五个方法、全部状态、phone 角色参数——都不存在。"""
    violations = _sim_launch_path_violations(SUPERVISOR_SRC.read_text(encoding="utf-8"))
    assert not violations, (
        "supervisor.py 里仍然残留着模拟器执行路径（产品运行期不允许有）：\n  - "
        + "\n  - ".join(violations))


def test_the_removal_checker_rejects_the_pre_removal_launch_path() -> None:
    """**这条测试的全部价值**：删之前那套写法必须被判红，而注释不许被误伤。

    没有它，"不存在"可能只是因为检查器什么都没在查。
    """
    old_style = textwrap.dedent('''
        import sim_phone
        import sim_provision

        class S:
            def __init__(self):
                self.sim_enabled = True
                self.sim_phone = None
                self._sim_lock = threading.Lock()

            def start(self):
                if self.sim_enabled:
                    threading.Thread(target=self._start_sim_phone, daemon=True).start()

            def _start_sim_phone(self):
                self.sim_phone = Child("sim-phone", ["--role=phone"], ".", {})

            def status(self):
                payload["simulation"] = self.sim_metrics.to_dict()
    ''')
    violations = _sim_launch_path_violations(old_style)
    assert violations, "删之前那套写法必须判红，否则这条契约什么都没守住"
    joined = "\n".join(violations)
    for token in ("sim_phone", "_start_sim_phone", "sim_enabled", "sim-phone",
                  "--role=phone", "simulation"):
        assert token in joined, f"检查器漏报了 {token!r}：{violations}"

    # 注释里出现同样的词**不许**误伤 —— 否则会逼人删掉解释性注释。
    commented = textwrap.dedent('''
        # 这里曾经 import sim_phone 并以 --role=phone 起第二个 Electron，
        # /status 曾经有 simulation 段，已整体移除（见 _REMOVED_SIM_ENV）。
        X = 1
    ''')
    assert _sim_launch_path_violations(commented) == [], (
        "注释里的同一个词不得被判为代码残留")


# --- 2. 行为（承重）：环境变量还在时"看得见但不照做"-------------------------


class _RecordingChild:
    """记录被构造出来的子进程（名字 + argv），不真的起进程。"""

    def __init__(self, name, argv, cwd, extra_env, **kwargs) -> None:
        self.name = name
        self.argv = list(argv)
        self.kwargs = dict(kwargs)
        self.tail: list[str] = []
        self.exit_code = None
        self.starts = 0
        self.pid = None
        self.liveness = kwargs.get("liveness", True)
        self.constructed.append(name)

    constructed: list[str] = []

    def start(self) -> None:
        self.starts += 1

    def alive(self) -> bool:
        return True

    def reap(self):
        return None

    def signal(self, _sig) -> None:
        return None

    def describe(self) -> dict:
        return {"alive": True, "pid": self.pid, "starts": self.starts,
                "exit_code": None, "output_tail": list(self.tail)}


@pytest.fixture
def clean_sim_env(monkeypatch):
    """先把已移除的那批变量全部清掉，保证断言不受本机环境影响。"""
    module = _load_supervisor()
    for name in module._REMOVED_SIM_ENV:
        monkeypatch.delenv(name, raising=False)
    return module


def test_sim_env_var_is_ignored_and_reported_not_acted_on(clean_sim_env, monkeypatch, caplog):
    """**承重的一条**：`BRIDGE_SIM_PHONE=true` 在环境里也不能起模拟器，但必须看得见。

    不 raise、不 exit —— 陈旧的生产环境变量不得把服务打成崩溃重启循环；也不许静默 ——
    容器 stdout 不进可检索日志，所以同时进 `/status.ignored_env`（只报变量名，不带取值）。
    """
    module = clean_sim_env
    monkeypatch.setenv("BRIDGE_SIM_PHONE", "true")
    monkeypatch.setenv("SIM_DEVICE_CREDENTIAL", "deadbeef.secret")

    _RecordingChild.constructed = []
    monkeypatch.setattr(module, "Child", _RecordingChild)

    with caplog.at_level(logging.WARNING):
        sup = module.BridgeSupervisor()
        # start() 里也别无选择：音频/TLS 两个前置副作用替掉，只观察子进程构造。
        monkeypatch.setattr(sup, "_materialize_bridge_tls", lambda: None)
        monkeypatch.setattr(sup, "_start_audio", lambda: None)
        sup.start()

    assert _RecordingChild.constructed == ["rtc_bridge", "sidecar"], (
        f"容器里只能有 rtc_bridge + sidecar 两个子进程，实得 {_RecordingChild.constructed}")
    assert not any("--role=phone" in " ".join(getattr(c, "argv", []))
                   for c in (sup.bridge, sup.sidecar))

    # 告警必须出现，且点名是哪些变量（取值绝不带出）。
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    joined = "\n".join(warnings)
    assert "BRIDGE_SIM_PHONE" in joined, f"必须告警点出残留变量，实得 {warnings}"
    assert "deadbeef.secret" not in joined, "告警里绝不许出现凭证取值"

    payload = sup.status()
    assert payload["ignored_env"] == ["BRIDGE_SIM_PHONE", "SIM_DEVICE_CREDENTIAL"], (
        "残留配置必须能从 /status 读到（容器 stdout 不进可检索日志，只打日志等于没报）："
        f"实得 {payload.get('ignored_env')!r}")
    assert "deadbeef.secret" not in str(payload), "/status 里绝不许出现凭证取值"


def test_status_never_advertises_simulation(clean_sim_env, monkeypatch):
    """`/status` 不再有 simulation 段 —— 无论环境变量怎么设。"""
    module = clean_sim_env
    monkeypatch.setenv("BRIDGE_SIM_PHONE", "true")
    monkeypatch.setenv("SIM_DEVICE_ID", "jax-sim-phone")

    sup = module.BridgeSupervisor()
    sup.bridge = _RecordingChild("rtc_bridge", [], "", {})
    sup.sidecar = _RecordingChild("sidecar", [], "", {})
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")

    payload = sup.status()
    assert "simulation" not in payload, (
        "simulation 段是「模拟器在容器里跑」的对外承诺，产品运行期不得再有")
    assert not hasattr(sup, "sim_phone"), "supervisor 不得再有 sim_phone 子进程句柄"
    assert not hasattr(sup, "sim_enabled"), "sim_enabled 这个开关本身也必须消失（否则它迟早被接回去）"
    # 部署门禁读的是这些键，移除 simulation 不得动它们。
    for key in ("ok", "rtc_bridge", "sidecar", "rtc_bridge_health", "trtc_sdk_version"):
        assert key in payload, f"/status 少了门禁依赖的键：{key}"
    assert payload["ok"] is True, "两个 liveness 子进程都活着时 ok 必须为 True"


# --- 3. 廉价保险：模块必须留下（容器外的本地 harness 还要用）-----------------


def test_sim_modules_survive_for_the_local_harness() -> None:
    """模块不许被"顺手清理"：`scripts/sim/` 的本地 harness 依赖它们。

    `scripts/sim/run-phone.py` 做 `sys.path.insert(ROOT/"cloudbridge")` 后
    `import sim_phone, sim_provision`，是容器之外唯一的端到端验证手段。
    """
    for name in ("sim_phone.py", "sim_provision.py"):
        assert (CLOUDBRIDGE / name).is_file(), f"{name} 被删了 —— 本地 harness 会直接 import 失败"

    harness = (ROOT / "scripts" / "sim" / "run-phone.py").read_text(encoding="utf-8")
    assert "import sim_phone" in harness and "import sim_provision" in harness, (
        "run-phone.py 是这两个模块的唯一消费者；它不再 import 就说明移除做过头了")

    spec = importlib.util.spec_from_file_location("jax_voice_bridge_sim_phone_kept",
                                                  CLOUDBRIDGE / "sim_phone.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert callable(module.parse_phone_log)

    sys.path.insert(0, str(CLOUDBRIDGE))
    import sim_provision  # noqa: PLC0415 - 这条断言本身就是"它能被 import"

    assert hasattr(sim_provision, "resolve_sim_device")
