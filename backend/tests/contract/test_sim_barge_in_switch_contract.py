"""契约：`SIM_BARGE_IN_DISABLE` 这个「停止脚手架自己打断回复」的唯一闸门必须有测试钉住。

为什么需要（2026-09-16 审计）
----------------------------
`sidecar/phone.js:181` 在收到**首包回复帧**时就调 `scheduleBargeIn()`，于是每轮模拟的
回复都会在首帧后 800ms 被自己推的插话打断。`scripts/sim/run-phone.py:37-42` 的注释记录：
43 次回复里 **29 次被真实打断**（近期几乎 100%）。⇒ 所有「回复不完整 / 像是 3 倍速」的
读数都可能是**我们自己的测量脚手架**造成的，而不是产品缺陷。

关掉这件事只有一个开关。而此前改坏它（拼写、取值集合、注入位置）**没有任何用例会红**：
全仓 grep `SIM_BARGE_IN_DISABLE|_barge_in_disabled` 在 `backend/tests` 与 `sidecar/test`
**零命中**。闸门无守护 = 下次有人"顺手清理"掉 `on` 或写错比较方向，测量结果会静默失真。

⚠️ 本文件**只加测试**，刻意**不统一**仓库里那四套互不相同的真值集合：
  · `scripts/sim/run-phone.py:31`              `{"1","true","yes","on"}`
  · `backend/rtc_bridge/config.py:106-108`     `{"1","true","yes"}`
  · `backend/rtc_bridge/config.py:111-113`     `not in {"0","false","no","off"}`
  · `cloudbridge/supervisor.py:168`            `not in {"0","false","no"}`
只有 run-phone 认 `on`。统一它们会牵动产品运行时代码，属另一件事。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SIM = ROOT / "scripts" / "sim"


def _load(name: str, path: Path):
    """加载 `scripts/sim/` 下的连字符脚本（不是合法模块名，只能走 importlib）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run_phone():
    return _load("sim_run_phone_under_test", SIM / "run-phone.py")


# --- ① run-phone._barge_in_disabled() 的取值集合 ------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_run_phone_truthy_values_disable_barge_in(monkeypatch, value) -> None:
    """`1/true/yes/on` 都必须被认成「本轮不测打断」。

    `on` 是最容易漏的一个：它是 run-phone 与另外三套集合的**唯一差异项**，
    漏掉它 ⇒ 用 `SIM_BARGE_IN_DISABLE=on` 的轮次仍会被自家插话截断，
    而读数看起来"合法"。大小写与首尾空白必须与实现同为 `.strip().lower()`。
    """
    mod = _run_phone()
    monkeypatch.setenv("SIM_BARGE_IN_DISABLE", value)
    assert mod._barge_in_disabled() is True, (
        f"SIM_BARGE_IN_DISABLE={value!r} 必须判为「本轮不测打断」"
    )

    # 大小写 / 空白：实现声称 `.strip().lower()`，必须真的容错
    monkeypatch.setenv("SIM_BARGE_IN_DISABLE", f"  {value.upper()}  ")
    assert mod._barge_in_disabled() is True, (
        f"SIM_BARGE_IN_DISABLE={value.upper()!r}（带空白）也必须判为真"
    )


@pytest.mark.parametrize("value", ["0", "false", "", "   ", "no", "off"])
def test_run_phone_falsy_or_unset_keeps_barge_in_on(monkeypatch, value) -> None:
    """`0/false/空` 以及**未设置**时必须保持打断注入（默认行为不得被改掉）。"""
    mod = _run_phone()
    monkeypatch.setenv("SIM_BARGE_IN_DISABLE", value)
    assert mod._barge_in_disabled() is False, (
        f"SIM_BARGE_IN_DISABLE={value!r} 不得判为真 —— 默认必须继续测打断"
    )

    monkeypatch.delenv("SIM_BARGE_IN_DISABLE", raising=False)
    assert mod._barge_in_disabled() is False, "变量未设置时默认行为必须是「测打断」"


# --- ② measure-rate-repeat 的注入方向 ----------------------------------------


class _FakeSubprocess:
    """替身：拦下 `subprocess.run` 只为**读出注入给子进程的环境**，不真的跑一轮 90s。"""

    STDOUT = subprocess.STDOUT

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return types.SimpleNamespace(returncode=0)


def _prep_repeat(monkeypatch, tmp_path):
    """把 measure-rate-repeat 的所有真实 I/O 换成 tmp 目录 / 空读数。"""
    mod = _load("sim_measure_rate_repeat_under_test", SIM / "measure-rate-repeat.py")
    monkeypatch.setattr(mod, "D", tmp_path)
    monkeypatch.setattr(mod, "metrics", lambda: {})
    monkeypatch.setattr(mod, "model_seconds", lambda: None)
    monkeypatch.setattr(mod, "age_drop_lines", lambda: [])
    monkeypatch.setattr(mod, "barge_stop_lines", lambda: [])
    monkeypatch.setattr(mod, "reply_text", lambda: "")
    (tmp_path / "e2e-summary.json").write_text('{"bridge_metrics": {}}', encoding="utf-8")
    fake = _FakeSubprocess()
    monkeypatch.setattr(mod, "subprocess", fake)
    return mod, fake


def test_clean_round_injects_disable_and_does_not_pollute_parent_env(monkeypatch, tmp_path) -> None:
    """clean（语速/完整度）轮**必须注入** `SIM_BARGE_IN_DISABLE=1`，且只能改副本。

    不注入 ⇒ 量到的是"被自己插话截断的拼接体"，语速比必然偏快（这正是当初
    「像 3 倍速」的来源之一）。只改副本 ⇒ 否则第二轮（barge）会被上一轮污染。
    """
    mod, fake = _prep_repeat(monkeypatch, tmp_path)
    monkeypatch.delenv("SIM_BARGE_IN_DISABLE", raising=False)

    mod.run_once(1, barge=False)

    env = fake.calls[0]["env"]
    assert env["SIM_BARGE_IN_DISABLE"] == "1", "clean 轮必须注入停用开关"
    assert env is not os.environ, "必须传副本，不得直接改父进程环境"
    assert "SIM_BARGE_IN_DISABLE" not in os.environ, "父进程环境不得被污染"


def test_barge_round_removes_disable_switch(monkeypatch, tmp_path) -> None:
    """barge（打断延迟）轮**必须移除**它，否则一个打断也量不到。"""
    mod, fake = _prep_repeat(monkeypatch, tmp_path)
    monkeypatch.setenv("SIM_BARGE_IN_DISABLE", "1")

    mod.run_once(1, barge=True)

    env = fake.calls[0]["env"]
    assert "SIM_BARGE_IN_DISABLE" not in env, (
        "barge 轮必须移除 SIM_BARGE_IN_DISABLE，否则本轮不会产生任何插话"
    )
