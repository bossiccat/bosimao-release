r"""契约：现场取证进程探针（`scripts/field-evidence/psprobe.py`）不得"静默给 0"。

背景（2026-09-19，claim `windows-popup-free`）
--------------------------------------------
上一轮装机 E2E 里，`installed_e2e.py` 的进程探针报「侧车 0 个」，而**同一时刻**
侧车日志正在以 500ms 打点。两组对照把结论钉死了：

    · `proc_probe_live.py`：同一个 probe() 内，v3 看到 5 个进程，v1/v4/v5/v6 看到 0 个；
    · `field_probe.py`：把"带不带 ExecutablePath""-Command 还是 -EncodedCommand"
      拆成 7 条 —— **全都正常看到 5 个**，包括 v1 的等价写法。

⇒ 不是"某字段不能用"，而是**同一命令串在两次运行里给出不同结果**：
这套取法不可复现，任何单次调用的结果都不可作证据。

修法是两条，缺一不可：
    1) 脚本落 `.ps1` 用 `-File` 执行（命令行里不再出现脚本正文引号），
       输出走 base64（stdout 纯 ASCII，不受代码页影响 —— 路径里有"贾克斯·星核"）；
    2) **探针自带阳性对照 + 双源交叉**，并把"探针自己可信吗"一起返回。
       **取不到阳性对照时返回 `trusted=False`，而不是返回 0** ——
       0 与"看不见"必须能区分。

本文件的立场
------------
第 (2) 条是 `psprobe.py` 存在的**唯一理由**。一个会在对照缺失时安静地返回 0 的探针，
和当初那个假阴性探针没有区别，而且更危险：它现在会以"通过"的样子出现。
所以本文件的核心断言不是"探针输出了什么"，而是：

    **把"取不到对照 ⇒ trusted=False"这一条改掉，测试必须变红。**

最后一条用例就是这条变异验证，且它自身也会校验"变异确实生效"——
否则一个改不动源文件的变异会静默通过，变成新的假绿。

所有用例都通过**注入合成的提供程序输出**来构造场景，不依赖本机是否装了 PowerShell、
也不依赖本机恰好有 jax 进程，因此在任何平台（含 ubuntu CI）都确定性可跑。
唯一需要 PowerShell 的是"整条命令真的能跑通"那一层 —— 那属于 Windows 现场验收，
不在本文件的守备范围内（本文件守的是**判据**，不是**通道**）。
"""
from __future__ import annotations

import base64
import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO_ROOT / "scripts" / "field-evidence" / "psprobe.py"

# 变异点：唯一那条"阳性对照失败"理由。把它删掉，探针就会在看不见自己的情况下
# 仍宣布 trusted=True —— 也就是"静默给 0"的翻版。
_MUTATION_TARGET = (
    '    if res["control_powershell"] < 1:\n'
    '        reasons.append("阳性对照失败：连 powershell.exe 自己都看不见")\n'
)


def _load_probe_from_path(path: Path):
    spec = importlib.util.spec_from_file_location(f"psprobe_under_test_{path.stem}_{id(path)}", path)
    assert spec and spec.loader, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_probe(tmp_path: Path, source: str | None = None):
    """加载一个探针模块实例；`source` 给定时为变异版。

    `tempfile` 被换成指向 tmp_path 的假模块，测试不往真实 %TEMP% 里写东西。
    """
    if source is None:
        module = _load_probe_from_path(PROBE_PATH)
    else:
        mutated = tmp_path / "psprobe_mutated.py"
        mutated.write_text(source, encoding="utf-8")
        module = _load_probe_from_path(mutated)
    module.tempfile = types.SimpleNamespace(gettempdir=lambda: str(tmp_path))
    return module


def _fake_run_factory(payload: dict | None, calls: list):
    """返回一个假的 subprocess.run：记录 argv，吐出合成 JSON。"""

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        stdout = b"" if payload is None else json.dumps(payload).encode("utf-8")
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    return _fake_run


def _inject(monkeypatch, module, payload, calls):
    """只替换**该模块实例**的 subprocess，不动真实 subprocess 模块。"""
    monkeypatch.setattr(module, "subprocess", types.SimpleNamespace(
        run=_fake_run_factory(payload, calls)), raising=False)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


_JA_JP_PATH = "C:\\Users\\Administrator\\AppData\\Local\\贾克斯·星核\\jax-pet.exe"
_SIDECAR_PATH = "C:\\Users\\Administrator\\AppData\\Local\\贾克斯·星核\\jax-rtc-sidecar.exe"

HEALTHY_PAYLOAD = {
    "control_powershell": 18,
    "total_processes": 438,
    "cim_jax": [f"{56056}|jax-pet.exe|{_b64(_JA_JP_PATH)}",
                f"{45212}|jax-rtc-sidecar.exe|{_b64(_SIDECAR_PATH)}"],
    "cim_jax_like": [],
    "gp_jax": [f"{56056}|jax-pet.exe|{_b64(_JA_JP_PATH)}",
               f"{45212}|jax-rtc-sidecar.exe|{_b64(_SIDECAR_PATH)}"],
}

# 阳性对照挂了：连 powershell.exe 自己都看不见。这正是当初那次假阴性的形状。
NO_CONTROL_PAYLOAD = {
    "control_powershell": 0,
    "total_processes": 438,
    "cim_jax": [],
    "cim_jax_like": [],
    "gp_jax": [],
}

# 双源不一致：CIM 看得见、Get-Process 看不见（半条通道）
MISMATCH_PAYLOAD = {
    "control_powershell": 18,
    "total_processes": 438,
    "cim_jax": HEALTHY_PAYLOAD["cim_jax"],
    "cim_jax_like": [],
    "gp_jax": [],
}


# ---------------------------------------------------------------------------
# 1. 核心契约：取不到阳性对照 ⇒ trusted=False，而不是 0
# ---------------------------------------------------------------------------
def test_missing_positive_control_yields_untrusted_not_zero(monkeypatch, tmp_path: Path) -> None:
    """**本文件存在的理由。**

    阳性对照取不到时，探针必须自曝不可信，而不能把"看不见"报成"一个都没有"。
    0 和看不见必须能区分 —— 这正是上一轮装机 E2E 翻车的地方。
    """
    module = _load_probe(tmp_path)
    calls: list = []
    _inject(monkeypatch, module, NO_CONTROL_PAYLOAD, calls)

    res = module.probe_jax()

    assert res["trusted"] is False, "取不到阳性对照却宣布可信 —— 这就是会静默给 0 的探针"
    assert res["why_untrusted"], "判不可信必须给出理由，否则调用方无法处置"
    assert "阳性对照" in res["why_untrusted"], res["why_untrusted"]
    # 调用方必须能区分"没找到"与"没看见"：不可信时不得把 jax 列表当成事实读数
    assert res["control_powershell"] == 0
    assert res["jax"] == []


def test_healthy_double_source_is_trusted(monkeypatch, tmp_path: Path) -> None:
    """阳性对照：健康的双源一致必须判 trusted=True。

    没有这一条，"恒 False"也会被当合格 —— 那样探针就再也不敢说任何结论了。
    """
    module = _load_probe(tmp_path)
    calls: list = []
    _inject(monkeypatch, module, HEALTHY_PAYLOAD, calls)

    res = module.probe_jax()

    assert res["trusted"] is True, res["why_untrusted"]
    assert res["why_untrusted"] == ""
    assert res["control_powershell"] == 18
    assert len(res["jax"]) == 2
    # 非 ASCII 路径必须能穿过 base64 无损回来
    assert "贾克斯·星核" in res["jax"][0]["path"]


def test_double_source_mismatch_is_untrusted(monkeypatch, tmp_path: Path) -> None:
    """半条通道（CIM 有、Get-Process 无）不得当成可信读数。"""
    module = _load_probe(tmp_path)
    calls: list = []
    _inject(monkeypatch, module, MISMATCH_PAYLOAD, calls)

    res = module.probe_jax()

    assert res["trusted"] is False
    assert "双源" in res["why_untrusted"], res["why_untrusted"]


def test_non_json_stdout_is_untrusted_not_zero(monkeypatch, tmp_path: Path) -> None:
    """stdout 不是 JSON（命令跑了但什么都没发生）⇒ 不可信，不是 0。"""
    module = _load_probe(tmp_path)
    _inject(monkeypatch, module, None, [])

    res = module.probe_jax()

    assert res["trusted"] is False
    assert res["jax"] == []
    assert res["why_untrusted"]


# ---------------------------------------------------------------------------
# 2. 命令行形态：必须 -File，不得 -Command / -EncodedCommand
# ---------------------------------------------------------------------------
def test_probe_invokes_powershell_with_file_not_command(monkeypatch, tmp_path: Path) -> None:
    """脚本正文不得出现在命令行里。

    `-Command` 路径上叠了三层引号语义（Python list2cmdline → CommandLineToArgvW →
    PowerShell 再解析一遍），正是当初"命令跑了、rc=0、输出却是空的"的载体。
    """
    module = _load_probe(tmp_path)
    calls: list = []
    _inject(monkeypatch, module, HEALTHY_PAYLOAD, calls)

    module.probe_jax()

    assert len(calls) == 1, calls
    argv = calls[0]
    assert "-File" in argv, argv
    assert "-Command" not in argv, argv
    assert "-EncodedCommand" not in argv, argv
    # 命令行里不该出现任何脚本正文片段（引号、$、分号）
    joined = " ".join(argv)
    assert "$" not in joined, joined
    assert ";" not in joined, joined
    # 落盘的 .ps1 必须真的存在
    ps1 = Path(argv[argv.index("-File") + 1])
    assert ps1.exists() and ps1.suffix == ".ps1"


# ---------------------------------------------------------------------------
# 3. 编码纪律：PS 模板纯 ASCII，落盘带 BOM，路径走 base64
# ---------------------------------------------------------------------------
def test_ps_template_is_pure_ascii() -> None:
    """内嵌的 PowerShell 模板必须纯 ASCII —— 否则又回到代码页赌博。"""
    module = _load_probe_from_path(PROBE_PATH)

    assert module.PS.isascii(), "PS 模板含非 ASCII，代码页一变就坏"
    assert "ToBase64String" in module.PS
    # 只允许单向：UTF8 编码进 base64（禁止反向解码，避免把代码页问题带回来）
    assert "UTF8.GetString" not in module.PS


def test_ps1_file_is_written_with_utf8_bom(tmp_path: Path) -> None:
    """PS 5.1 靠 BOM 判 UTF-8；没有 BOM 会按 ANSI 读，中文注释变乱码并把解析搞坏。"""
    module = _load_probe(tmp_path)

    ps1 = module._ps1_path()

    assert ps1.exists()
    assert ps1.read_bytes()[:3] == b"\xef\xbb\xbf", "缺 UTF-8 BOM"


def test_decode_round_trips_non_ascii_path() -> None:
    """base64 必须能把非 ASCII 路径原样带回来。"""
    module = _load_probe_from_path(PROBE_PATH)

    pid, name, got = module._decode(f"1234|jax-rtc-sidecar.exe|{_b64(_SIDECAR_PATH)}")

    assert (pid, name, got) == ("1234", "jax-rtc-sidecar.exe", _SIDECAR_PATH)


# ---------------------------------------------------------------------------
# 4. 变异验证：把"取不到对照 ⇒ trusted=False"改掉，契约必须变红
# ---------------------------------------------------------------------------
def test_mutation_making_absent_control_silently_trusted_turns_contract_red(
    monkeypatch, tmp_path: Path
) -> None:
    """**这条用例是 `psprobe.py` 存在的唯一理由的可执行版本。**

    把"阳性对照失败 ⇒ 判不可信"那一条删掉，探针就会在看不见自己的情况下
    仍然宣布 `trusted=True` 并给出一个 jax 列表 —— 也就是"静默给 0"的翻版。
    这里断言：变异之后，本文件第 1 条用例的判据**确实会红**。

    同时校验"变异确实生效"：如果源文件改了、替换没命中，
    变异版会与原件行为一致，此时本用例失败 —— 不允许一个改不动代码的变异静默通过。
    """
    original = PROBE_PATH.read_text(encoding="utf-8")
    assert original.count(_MUTATION_TARGET) == 1, (
        "变异目标在 psprobe.py 里不唯一或找不到 —— 变异未生效，本用例失去意义。"
        "若探针实现有变，请同步更新 _MUTATION_TARGET。"
    )
    mutated_source = original.replace(_MUTATION_TARGET, "")

    # (a) 原件：阳性对照缺失 ⇒ 不可信（第 1 条用例的判据成立）
    (tmp_path / "clean").mkdir(parents=True, exist_ok=True)
    clean = _load_probe(tmp_path / "clean")
    _inject(monkeypatch, clean, NO_CONTROL_PAYLOAD, [])
    assert clean.probe_jax()["trusted"] is False

    # (b) 变异版：同一场景下**必须**变成可信 —— 证明变异真的改变了行为
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir(parents=True, exist_ok=True)
    mutated = _load_probe(mutated_dir, source=mutated_source)
    _inject(monkeypatch, mutated, NO_CONTROL_PAYLOAD, [])
    mutated_res = mutated.probe_jax()
    assert mutated_res["trusted"] is True, (
        "变异未生效（行为与原件相同）⇒ 本变异验证是假的，必须修 _MUTATION_TARGET"
    )

    # (c) 于是第 1 条用例的断言在变异版上确实会失败 —— 契约有牙齿
    with pytest.raises(AssertionError):
        assert mutated_res["trusted"] is False, (
            "取不到阳性对照却宣布可信 —— 这就是会静默给 0 的探针"
        )
