#!/usr/bin/env python3
"""O-018 切片 2 安装器契约独立校验器（静态）。

校验「NSIS 受管 launcher 接入」的全部静态契约（docs/plans/2026-08-10-o018-slice2-design.md Task 4）：

    1. hooks 文件存在且定义 NSIS_HOOK_POSTINSTALL 宏；
    2. NSIS 只以固定路径调用 launcher（$INSTDIR\\provision_sidecar_credential_launcher.exe），
       全文件恰一次 ExecWait，零参数（Secret 不得出现在 argv）；
    3. helper（provision_sidecar_credential.exe）不得被 NSIS 直接调用——它只能由
       launcher 经继承 stdin 匿名管道 CreateProcess 拉起（切片 2 架构红线）；
    4. fail-closed：launcher 非 0 退出码 → Abort 安装（IntCmp + Abort 模式）；
    5. 安装器输入零 Secret 字面量（32+ hex 串 / ReadEnvStr / SECRET 宏定义均禁止）；
    6. tauri.conf.json：nsis.installMode == "currentUser"（per-user 安装边界）；
    7. tauri.conf.json：nsis.installerHooks 指向 hooks 文件（接线锁定）；
    8. externalBin 随包发布 launcher（$INSTDIR 存在固定路径的前提）。

用法：
    python scripts/verify-o018-installer-contract.py
退出码：0 = PASS；1 = FAIL。stdout 逐条输出 `<PASS|FAIL> <CHECK>`，
末行输出稳定标记 `O018_INSTALLER_CONTRACT=PASS|FAIL`（供 pytest/CI 消费，勿本地化）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_REL = Path("pet-ui/src-tauri/installer/o018-installer-hooks.nsh")
CONF_REL = Path("pet-ui/src-tauri/tauri.conf.json")

LAUNCHER_NAME = "provision_sidecar_credential_launcher.exe"
HELPER_NAME = "provision_sidecar_credential.exe"  # 无 _launcher 后缀
LAUNCHER_INVOCATION = f'"$INSTDIR\\{LAUNCHER_NAME}"'
HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{32,}")


def _checks() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    hooks_path = REPO_ROOT / HOOKS_REL
    hooks_text: str = ""
    if hooks_path.is_file():
        hooks_text = hooks_path.read_text(encoding="utf-8")
    checks.append(("HOOKS_FILE_EXISTS", hooks_path.is_file(), str(HOOKS_REL)))

    checks.append((
        "HOOK_DEFINES_POSTINSTALL",
        "!macro NSIS_HOOK_POSTINSTALL" in hooks_text,
        "hooks 须定义 !macro NSIS_HOOK_POSTINSTALL",
    ))

    exec_waits = [
        line.strip() for line in hooks_text.splitlines() if "ExecWait" in line
    ]
    checks.append((
        "SINGLE_FIXED_LAUNCHER_INVOCATION",
        len(exec_waits) == 1 and LAUNCHER_INVOCATION in exec_waits[0],
        f"恰一次 ExecWait 且目标为 {LAUNCHER_INVOCATION}（零参数固定路径）",
    ))

    helper_direct = [
        line.strip()
        for line in hooks_text.splitlines()
        if ("Exec" in line and HELPER_NAME in line and LAUNCHER_NAME not in line)
    ]
    checks.append((
        "HELPER_NOT_DIRECTLY_INVOKED",
        not helper_direct,
        "helper 只能由 launcher 经管道拉起，NSIS 不得直接 Exec",
    ))

    fail_closed = (
        re.search(r"IntCmp\s+\$R0\s+0\s", hooks_text) is not None
        and re.search(r"^\s*Abort\s*$", hooks_text, re.MULTILINE) is not None
    )
    checks.append((
        "FAIL_CLOSED_ABORT",
        fail_closed,
        "launcher 非 0 退出必须 Abort（IntCmp $R0 0 + Abort）",
    ))

    no_secret = (
        HEX_RUN_RE.search(hooks_text) is None
        and "ReadEnvStr" not in hooks_text
        and "SECRET" not in hooks_text.upper().replace("SIDECAR", "")
    )
    checks.append((
        "NO_SECRET_LITERALS",
        no_secret,
        "安装器输入禁止 32+ hex 串 / ReadEnvStr / SECRET 宏",
    ))

    conf: dict = {}
    conf_path = REPO_ROOT / CONF_REL
    if conf_path.is_file():
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
    nsis = (
        conf.get("bundle", {}).get("windows", {}).get("nsis", {})
        if isinstance(conf, dict)
        else {}
    )

    checks.append((
        "CONF_PER_USER",
        nsis.get("installMode") == "currentUser",
        'tauri.conf bundle.windows.nsis.installMode 须为 "currentUser"',
    ))
    checks.append((
        "CONF_HOOKS_WIRED",
        nsis.get("installerHooks") == "./installer/o018-installer-hooks.nsh",
        'tauri.conf bundle.windows.nsis.installerHooks 须指向 "./installer/o018-installer-hooks.nsh"',
    ))

    external_bin = conf.get("bundle", {}).get("externalBin", []) if isinstance(conf, dict) else []
    checks.append((
        "EXTERNAL_BIN_SHIPS_LAUNCHER",
        "binaries/provision_sidecar_credential_launcher" in external_bin,
        "externalBin 须随包发布 launcher（$INSTDIR 固定路径前提）",
    ))

    return checks


def main() -> int:
    results = _checks()
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, why in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {why}")
    verdict = "PASS" if not failed else "FAIL"
    print(f"O018_INSTALLER_CONTRACT={verdict}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
