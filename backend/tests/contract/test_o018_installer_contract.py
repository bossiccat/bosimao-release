r"""契约：O-018 切片 2 NSIS 受管 launcher 安装器接入（静态腿）。

背景（2026-09-03 切片 2 启动，docs/plans/2026-08-10-o018-slice2-design.md Task 4）：
    切片 3 已选方案 A（jax-services.ps1 编排）并闭环；切片 2 归位到商业安装包
    阶段——fresh install 时由 NSIS 安装器在最终交互用户上下文调用受管 launcher，
    经匿名管道向一次性 provisioner 供给同值 opaque credential。

架构红线（锁定，违反 = 退回）：
    - NSIS 只以固定路径、零参数调用 launcher（Secret 不进 argv / 环境变量 / 文件）；
    - helper（provision_sidecar_credential.exe）只能由 launcher 经继承 stdin 管道
      CreateProcess 拉起，NSIS 不得直接 Exec helper；
    - launcher 非 0 退出码 → Abort 安装（fail-closed，不留半供给状态）；
    - per-user 安装（installMode=currentUser），安装器输入零 Secret 字面量。

实现路径（与切片 2 计划的最小差异，已论证）：
    不 fork 整套 NSIS 模板（08-10 旧自用 .nsi 路径已被 tauri 生成式安装器取代，
    build6 实证 target/release/nsis/x64/installer.nsi 含 NSIS_HOOK_POSTINSTALL
    插槽），改用 tauri 2 官方 nsis.installerHooks 机制——同一契约，侵入面最小。

测试腿（本文件全部为静态契约；真实安装/干净机 E2E 归 Task 5 与外部条件）：
    腿 1：独立校验器脚本存在且可执行（exit 0 + 稳定标记 PASS）；
    腿 2：校验器对当前仓库输出全 PASS（pytest 消费稳定 ASCII 标记，GBK 安全）；
    腿 3：关键接线点直接复核（hooks 文件存在 + tauri.conf 双键锁定），防校验器
        自身路径漂移。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER = REPO_ROOT / "scripts" / "verify-o018-installer-contract.py"
HOOKS = REPO_ROOT / "pet-ui" / "src-tauri" / "installer" / "o018-installer-hooks.nsh"
CONF = REPO_ROOT / "pet-ui" / "src-tauri" / "tauri.conf.json"


def test_verifier_script_exists_and_passes() -> None:
    assert VERIFIER.is_file(), f"缺少独立校验器: {VERIFIER}"
    result = subprocess.run(
        [sys.executable, str(VERIFIER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"安装器契约校验 FAIL（exit {result.returncode}）:\n{result.stdout}\n{result.stderr}"
    )
    assert "O018_INSTALLER_CONTRACT=PASS" in result.stdout, "缺少稳定 PASS 标记"


def test_verifier_reports_no_fail_lines() -> None:
    result = subprocess.run(
        [sys.executable, str(VERIFIER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    fail_lines = [line for line in result.stdout.splitlines() if line.startswith("FAIL ")]
    assert fail_lines == [], f"存在未通过契约项: {fail_lines}"


def test_hooks_and_conf_wiring_exist() -> None:
    assert HOOKS.is_file(), f"hooks 文件缺失: {HOOKS}"
    hooks_text = HOOKS.read_text(encoding="utf-8")
    assert "!macro NSIS_HOOK_POSTINSTALL" in hooks_text
    assert "provision_sidecar_credential_launcher.exe" in hooks_text

    import json

    conf = json.loads(CONF.read_text(encoding="utf-8"))
    nsis = conf["bundle"]["windows"]["nsis"]
    assert nsis["installMode"] == "currentUser"
    assert nsis["installerHooks"] == "./installer/o018-installer-hooks.nsh"
