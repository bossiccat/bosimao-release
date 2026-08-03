"""status_detector 变异验证脚本（核心交付：4 变异必须全杀）

用法（在 backend 目录下运行）：
    C:/Users/Administrator/.workbuddy/binaries/python/envs/monitor-app/Scripts/python.exe \
        tests/unit/mutation_verify/run_mutation_verify.py

原理：
    对 app/engine/status_detector.py 逐个注入 4 个语义等价变异（阈值 ±1），
    临时替换实现 → 跑 test_status_detector.py → 恢复原实现。
    若变异实现下测试失败（exit != 0）→ 变异被杀（KILLED）；
    若变异实现下测试全绿 → 变异存活（SURVIVED，说明测试有洞）。

安全（二进制 I/O，保证字节级精确恢复）：
    - 变异副本保存于 mutants/ 目录（含 _original_backup.py，字节级备份）。
    - 每次替换用 try/finally 保证恢复；恢复后用 sha256 校验与备份一致。
    - 若校验失败，说明实现文件被外部并发修改（如后端并行编辑），
      报告会标注 CONCURRENT-MODIFIED 警告，但变异裁决仍以 pytest 结果为准。
    - 绝不修改除 app/engine/status_detector.py 之外任何实现代码。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[2]                       # backend/
TARGET = BACKEND / "app" / "engine" / "status_detector.py"
TEST_FILE = BACKEND / "tests" / "unit" / "test_status_detector.py"
MUTANTS_DIR = HERE / "mutants"
REPORT = HERE / "mutation_report.txt"

PYTHON = sys.executable

# (变异名, 目标子串, 变异子串)
MUTATIONS = [
    (
        "frame_threshold_3_to_2",
        b"snapshot.stuck_frames >= cfg.stuck_frame_threshold",
        b"snapshot.stuck_frames >= cfg.stuck_frame_threshold - 1",
    ),
    (
        "frame_threshold_3_to_4",
        b"snapshot.stuck_frames >= cfg.stuck_frame_threshold",
        b"snapshot.stuck_frames >= cfg.stuck_frame_threshold + 1",
    ),
    (
        "timeout_120_to_121",
        b"elapsed >= cfg.stuck_timeout_seconds",
        b"elapsed >= cfg.stuck_timeout_seconds + 1",
    ),
    (
        "timeout_120_to_119",
        b"elapsed >= cfg.stuck_timeout_seconds",
        b"elapsed >= cfg.stuck_timeout_seconds - 1",
    ),
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_pytest() -> tuple[int, str]:
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", str(TEST_FILE), "-q"],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    MUTANTS_DIR.mkdir(parents=True, exist_ok=True)
    original_bytes = TARGET.read_bytes()
    original_sha = sha256(original_bytes)
    backup = MUTANTS_DIR / "_original_backup.py"
    backup.write_bytes(original_bytes)

    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("status_detector 变异验证报告")
    lines.append(f"目标实现: {TARGET}")
    lines.append(f"测试文件: {TEST_FILE}")
    lines.append(f"原实现 sha256: {original_sha}")
    lines.append("=" * 70)

    # 基线：原实现必须全绿，否则变异验证无意义
    rc, out = run_pytest()
    baseline_ok = rc == 0
    lines.append(f"\n[基线] 原实现 pytest exit={rc} -> {'PASS (全绿)' if baseline_ok else 'FAIL'}")
    if not baseline_ok:
        lines.append("基线失败，变异验证中止。请先修复测试或实现。")
        lines.append(out[-3000:])
        REPORT.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        return 2

    killed = 0
    survived: list[str] = []
    concurrent_warning = False
    lines.append("\n" + "-" * 70)
    for name, old, new in MUTATIONS:
        if original_bytes.count(old) != 1:
            lines.append(f"\n[{name}] 目标子串出现 {original_bytes.count(old)} 次（应为 1），跳过")
            survived.append(name)
            continue
        mutant_bytes = original_bytes.replace(old, new)
        mutant_file = MUTANTS_DIR / f"mutant_{name}.py"
        mutant_file.write_bytes(mutant_bytes)

        # 注入变异（临时替换实现）
        try:
            TARGET.write_bytes(mutant_bytes)
            rc, out = run_pytest()
        finally:
            # 无论成败必须恢复原实现（字节级）
            TARGET.write_bytes(original_bytes)

        # 恢复校验：与备份字节比对
        restored = sha256(TARGET.read_bytes()) == sha256(backup.read_bytes())
        if not restored:
            concurrent_warning = True
        status = "KILLED" if rc != 0 else "SURVIVED"
        if rc != 0:
            killed += 1
        else:
            survived.append(name)
        lines.append(f"\n[{status}] {name}")
        lines.append(f"    变异: {old.decode()}  ->  {new.decode()}")
        lines.append(
            f"    pytest exit={rc} | 恢复校验: {'OK' if restored else 'CONCURRENT-MODIFIED'}"
        )
        if status == "KILLED":
            # 截取失败断言摘要
            for ln in out.splitlines():
                if "FAILED" in ln or "assert" in ln or "Error" in ln:
                    lines.append(f"    {ln.strip()}")
                    if sum(1 for x in lines if x.startswith("    ")) > 30:
                        break

    lines.append("\n" + "=" * 70)
    if concurrent_warning:
        lines.append(
            "警告：至少一次恢复校验未通过 → 实现文件在验证期间被外部并发修改"
            "（本脚本已按备份字节强制恢复，请确认当前实现与备份一致）。"
        )
    lines.append(f"变异总杀：{killed}/{len(MUTATIONS)}")
    if survived:
        lines.append(f"存活变异（测试有洞）：{', '.join(survived)}")
    lines.append("=" * 70)
    verdict = "PASS：4 变异全杀，测试可防阈值/超时误改" if killed == len(MUTATIONS) else "FAIL：存在存活变异"
    lines.append(verdict)

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
