"""契约：语音质量门禁必须（a）阈值明确、（b）前置不满足时**拒绝**而不是"通过"、
（c）其依赖的模拟器必须**在仓库里**（否则门禁无法被任何人复现）。

为什么要有这条契约
------------------
2026-09-13：整套模拟器一直躺在 `tmp/`（被 `.gitignore:21` 忽略）。
后果是「语音三项指标已达标」这件事**无法被别人复现** —— 门禁脚本引用未跟踪文件，
换台机器/换个人就只剩一句结论。所以把 harness 移进 `scripts/sim/` 并用契约钉住。

同时钉住"宁可不报，不许假通过"：前置不满足（非 Windows / 缺 Electron / 缺 .env）
必须非零退出，绝不能降级成静态检查或静默跳过。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SIM = ROOT / "scripts" / "sim"
GATE = SIM / "check-voice-quality-gate.py"

SIM_FILES = (
    "run-rtc-bridge.py",
    "run-sidecar.py",
    "run-phone.py",
    "run-sim-e2e.py",
    "measure-rate-repeat.py",
    "check-voice-quality-gate.py",
)


def _load_gate():
    spec = importlib.util.spec_from_file_location("voice_quality_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_gate_and_harness_are_tracked_in_git() -> None:
    """门禁与它依赖的模拟器都必须在仓库里 —— 否则"可复现"是假的。"""
    for name in SIM_FILES:
        assert (SIM / name).is_file(), f"缺少 scripts/sim/{name}"
        proc = subprocess.run(["git", "ls-files", "--error-unmatch", f"scripts/sim/{name}"],
                              cwd=str(ROOT), capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"scripts/sim/{name} 未被 git 跟踪 —— 门禁引用未跟踪文件就不可复现"
            f"（stdout={proc.stdout.strip()} stderr={proc.stderr.strip()}）"
        )


def test_thresholds_are_explicit_and_locked() -> None:
    """阈值必须是有名字的常量且与实测基线一致，改动必须是有意识的决定。"""
    gate = _load_gate()
    assert gate.MIN_RATIO_SPEECH == 0.85, (
        "语速比下限来自实测：修复前 0.36×（下行丢 38% 音频），修复后 5 轮 0.91–1.04"
    )
    assert gate.REQUIRE_DOWNLINK_DROPS_ZERO is True
    assert gate.REQUIRE_BARGE_EMIT_MS is True


def test_refuses_on_non_windows_instead_of_passing(monkeypatch) -> None:
    """"前置不满足"必须拒绝运行，绝不允许静默变成"通过"。"""
    gate = _load_gate()
    monkeypatch.setattr(gate.sys, "platform", "linux")
    with pytest.raises(gate.GateRefusal):
        gate.preflight()
    assert gate.main() == 2, "非 Windows 上必须非零退出（2=拒绝），不能返回 0"


def test_refuses_when_electron_missing(monkeypatch) -> None:
    """缺 Electron ⇒ 跑出来的是假结果，必须拒绝。"""
    gate = _load_gate()
    monkeypatch.setattr(gate.sys, "platform", "win32")
    monkeypatch.setattr(gate, "ELECTRON", ROOT / "sidecar" / "nope" / "electron.exe")
    with pytest.raises(gate.GateRefusal):
        gate.preflight()


def test_low_ratio_is_a_failure_not_a_warning() -> None:
    """语速比低于阈值必须判 FAIL —— 这正是当初"像 3 倍速"的真身。"""
    gate = _load_gate()
    rows = [{"run": 1, "state": "replied", "reply_frames": 213, "ratio_speech": 0.36,
             "queue_drops_down": 216, "queue_drops_up": 0, "age_drop_lines": []}]
    fails, _warns = gate._check_clean(rows)
    assert any("语速比" in f for f in fails)
    assert any("queue_drops_down" in f for f in fails)


def test_missing_ratio_is_a_failure_not_skipped() -> None:
    """语速比不可得时不许"跳过算过"—— 宁可不报数。"""
    gate = _load_gate()
    rows = [{"run": 1, "state": "replied", "reply_frames": 100, "ratio_speech": None,
             "queue_drops_down": 0, "queue_drops_up": 0, "age_drop_lines": []}]
    fails, _warns = gate._check_clean(rows)
    assert any("不可得" in f for f in fails)


def test_missing_downlink_drops_is_a_failure_not_an_implicit_zero() -> None:
    """`queue_drops_down` **缺席**（None / 键不存在）必须判 FAIL，不能被当成 0。

    为什么（2026-09-16 审计出的"假绿主通道"）
    ----------------------------------------
    `check-voice-quality-gate.py:100` 原写法是
    `if REQUIRE_DOWNLINK_DROPS_ZERO and r.get("queue_drops_down"):` —— `None` 为假
    ⇒ **"没测到"被判成"零丢帧 = 通过"**。而同一个门禁里 :96-99 对 `ratio_speech is None`
    偏偏是**硬 FAIL**：同一个门禁两套缺席语义。

    触发路径零前置条件：`run-sim-e2e.py:167` 用 `urlopen("...:19093/metrics", timeout=5)`
    取桥指标，失败即 `:170-172` 置 `summary["bridge_metrics"] = None`；
    `measure-rate-repeat.py:120-123` 再 `or {}` 取默认 ⇒ 字段整体缺失。
    **一次 5s 读超时即可**，而那一轮可能真的跑成功了。
    """
    gate = _load_gate()
    rows = [{"run": 1, "state": "replied", "reply_frames": 100, "ratio_speech": 0.95,
             "queue_drops_up": 0, "age_drop_lines": []}]

    explicit_none = [dict(rows[0], queue_drops_down=None)]
    fails_none, _ = gate._check_clean(explicit_none)
    assert any("queue_drops_down" in f for f in fails_none), (
        f"queue_drops_down=None 必须 FAIL（缺席 ≠ 零丢帧），实得 {fails_none}"
    )

    absent_key = [dict(rows[0])]          # 键整体不存在（桥指标 `or {}` 后的真实形态）
    fails_absent, _ = gate._check_clean(absent_key)
    assert any("queue_drops_down" in f for f in fails_absent), (
        f"queue_drops_down 键缺席必须 FAIL，实得 {fails_absent}"
    )


def test_downlink_age_drop_fails_but_uplink_only_warns() -> None:
    """下行的帧龄丢弃 = 用户少听一帧内容；上行的帧龄丢弃语义上正确，只告警。"""
    gate = _load_gate()
    rows = [{"run": 1, "state": "replied", "reply_frames": 100, "ratio_speech": 0.95,
             "queue_drops_down": 0, "queue_drops_up": 22,
             "age_drop_lines": ["audio age-drop[up]: 丢弃 22 帧 / 队首帧龄 1032ms / 上限 1000ms"]}]
    fails, warns = gate._check_clean(rows)
    assert fails == [], f"上行丢帧不应判 FAIL，实测 {fails}"
    assert any("queue_drops_up" in w for w in warns)

    rows[0]["age_drop_lines"] = ["audio age-drop[down]: 丢弃 3 帧 / 队首帧龄 1200ms / 上限 30000ms"]
    fails2, _ = gate._check_clean(rows)
    assert any("[down]" in f for f in fails2), "下行帧龄丢弃必须判 FAIL"


def test_barge_without_bridge_log_is_a_failure() -> None:
    """打断的桥侧权威日志缺席 ⇒ FAIL（手机侧口径实测会给 null，不能拿来顶替）。"""
    gate = _load_gate()
    fails, _ = gate._check_barge([{"run": 1, "barge_stop": [], "barge_in_attempted": True}])
    assert any("权威口径缺席" in f for f in fails)

    fails2, _ = gate._check_barge([{
        "run": 1, "barge_in_attempted": True,
        "barge_stop": ["barge_in stop old_reply=x:1 frames=61 stop_ms=0 residual=158 reason=down_silent"]}])
    assert any("emit_stop_ms" in f for f in fails2), "裸 stop_ms= 是已废弃的错口径，必须判 FAIL"
