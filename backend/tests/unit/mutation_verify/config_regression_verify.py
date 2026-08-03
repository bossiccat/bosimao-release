"""config 层级 bug 回归验证（先红后绿证据）

背景：load_detection()/load_push() 曾对整个 YAML 文档 model_validate，
忽略顶层 `detection:` / `push:` 键 → 注入 5/180/3 恒输出默认 3/120/2。
后端本轮已修复（提取 doc["detection"] / doc["push"]）。

本脚本临时将 config.py 还原为"历史 buggy 版本"（整包 model_validate），
跑 test_config_loading.py 应 FAIL（红 = 测试能捕获该 bug），
再恢复修复版，应 PASS（绿）。由此给出 TDD 门禁的红→绿证据。

用法（在 backend 目录下运行）：
    python tests/unit/mutation_verify/config_regression_verify.py

安全：二进制 I/O，try/finally 强制恢复，恢复后校验 sha256 与备份一致。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[2]
TARGET = BACKEND / "app" / "config.py"
TEST_FILE = BACKEND / "tests" / "unit" / "test_config_loading.py"
BACKUP = HERE / "mutants" / "_config_original_backup.py"
REPORT = HERE / "config_regression_report.txt"
PYTHON = sys.executable

# 历史 buggy 替换：修复版 load_detection/load_push → 整包 model_validate（忽略顶层键）
BUGGY_PATCHES = [
    (
        b"def load_detection() -> DetectionConfig:\n    doc = _load_yaml(\"detection.yaml\")\n    return DetectionConfig.model_validate(doc[\"detection\"])",
        b"def load_detection() -> DetectionConfig:\n    return DetectionConfig.model_validate(_load_yaml(\"detection.yaml\"))",
    ),
    (
        b"def load_push() -> PushConfig:\n    doc = _load_yaml(\"push.yaml\")\n    return PushConfig.model_validate(doc[\"push\"])",
        b"def load_push() -> PushConfig:\n    return PushConfig.model_validate(_load_yaml(\"push.yaml\"))",
    ),
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", str(TEST_FILE), "-q"],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    original = TARGET.read_bytes()
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_bytes(original)

    lines = ["=" * 70, "config 层级 bug 回归验证（先红后绿）", f"目标: {TARGET}", f"测试: {TEST_FILE}", "=" * 70]

    # 构造 buggy 版本
    buggy = original
    for old, new in BUGGY_PATCHES:
        if buggy.count(old) != 1:
            lines.append(f"\n[跳过] 修复版子串出现 {buggy.count(old)} 次（应为 1）：{old.decode()[:60]}...")
        else:
            buggy = buggy.replace(old, new)

    # 阶段 1：buggy 实现 → 应红
    try:
        TARGET.write_bytes(buggy)
        rc_red, out_red = run_tests()
    finally:
        TARGET.write_bytes(original)

    restored_after_buggy = sha(TARGET.read_bytes()) == sha(original)
    lines.append(f"\n[阶段1] 历史 buggy 实现 pytest exit={rc_red} -> {'RED (测试捕获 bug ✅)' if rc_red != 0 else 'GREEN (测试未捕获 bug ❌)'}")
    lines.append(f"        恢复校验: {'OK' if restored_after_buggy else 'CONCURRENT-MODIFIED'}")
    for ln in out_red.splitlines():
        if "FAILED" in ln or "assert" in ln:
            lines.append(f"        {ln.strip()}")
            if len([x for x in lines if x.startswith('        ')]) > 20:
                break

    # 阶段 2：修复版实现 → 应绿
    rc_green, out_green = run_tests()
    lines.append(f"\n[阶段2] 修复版实现 pytest exit={rc_green} -> {'GREEN ✅' if rc_green == 0 else 'RED ❌'}")
    summary = out_green.strip().splitlines()
    if summary:
        lines.append(f"        {summary[-1].strip()}")

    lines.append("\n" + "=" * 70)
    ok = (rc_red != 0) and (rc_green == 0)
    lines.append("PASS：测试能捕获 config 层级 bug（红），修复后通过（绿）" if ok else "FAIL：红绿门未满足")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
