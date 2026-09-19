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

2026-09-19 追加（claim windows-popup-free，只增加判据，不改 1-8）：
    9. legacy watchdog 任务名三个齐全（Jax-Watchdog-AtStartup /
       Jax-Watchdog-Every5Min / jax-watchdog）——产品自己造的机器态必须自己收；
   10. 清理走 nsExec::ExecToLog（NSIS 内建 Exec 会给 CUI 子进程分配可见控制台）；
   11. 清理宏同时插在 NSIS_HOOK_POSTINSTALL 与 NSIS_HOOK_POSTUNINSTALL；
   12. 清理早于 fail-closed Abort（中止安装时陈旧任务也应已清除）；
   13. Abort 语句仍恰为 1 条（清理是完全非致命的，不得新增 Abort）。

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

# ---- legacy watchdog 计划任务清理（claim: windows-popup-free）----------------
# 产品早期版本注册过这三个计划任务；注册脚本已退役，但没有任何代码注销它们，
# 升级安装后它们仍每 5 分钟触发并尝试拉起已删除的控制台二进制。清理逻辑是
# 2026-09-19 加的，这几条 check 把它**锁住**，防止后人删掉。
LEGACY_TASK_NAMES = (
    "Jax-Watchdog-AtStartup",
    "Jax-Watchdog-Every5Min",
    "jax-watchdog",
)
CLEANUP_MACRO = "JAX_LEGACY_WATCHDOG_TASK_CLEANUP"
CLEANUP_INSERT = f"!insertmacro {CLEANUP_MACRO}"
NSEXEC_TOKEN = "nsExec::ExecToLog"
ABORT_STMT_RE = re.compile(r"^\s*Abort\s*$", re.MULTILINE)


def _macro_body(text: str, name: str) -> str:
    """取 `!macro <name>` 到其 `!macroend` 之间的正文（找不到返回空串）。"""
    start = text.find(f"!macro {name}")
    if start < 0:
        return ""
    end = text.find("!macroend", start)
    return text[start:] if end < 0 else text[start:end]


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

    # ---- legacy watchdog 计划任务清理（2026-09-19，claim windows-popup-free）----
    # 这几条只**追加**锁定，不改上面任何既有判据。
    #
    # 判据一律锚在**清理宏的正文**里，不锚全文：本文件的注释里也写着这三个任务名，
    # 只测 `name in hooks_text` 会被注释满足 —— 那是"有形状没牙齿"的锁。
    # （这个缺陷是变异 N1/N2 实测抓出来的，别再退回全文匹配。）
    cleanup_body = _macro_body(hooks_text, CLEANUP_MACRO)

    missing_names = [n for n in LEGACY_TASK_NAMES if n not in cleanup_body]
    checks.append((
        "LEGACY_TASK_NAMES_COMPLETE",
        bool(cleanup_body) and not missing_names,
        "清理宏正文必须覆盖全部三个 legacy 任务名: "
        + ", ".join(LEGACY_TASK_NAMES)
        + (f"（缺: {', '.join(missing_names)}）" if missing_names else ""),
    ))

    cleanup_lines = [l.strip() for l in cleanup_body.splitlines()]
    nsexec_stmts = [l for l in cleanup_lines if l.startswith(NSEXEC_TOKEN)]
    builtin_exec_stmts = [
        l for l in cleanup_lines
        if l.startswith("Exec ") or l.startswith("Exec\t") or l.startswith("nsExec::Exec ")
    ]
    checks.append((
        "LEGACY_CLEANUP_USES_NSEXEC",
        len(nsexec_stmts) == 1 and not builtin_exec_stmts,
        f"清理宏正文必须恰有一条 {NSEXEC_TOKEN} 语句、且不得使用会给 CUI 子进程"
        "分配可见控制台的 NSIS 内建 Exec（实测内建 Exec: HAS_CONSOLE=True，"
        f"nsExec: HAS_CONSOLE=False；当前 nsExec 语句 {len(nsexec_stmts)} 条，"
        f"内建 Exec 语句 {len(builtin_exec_stmts)} 条）",
    ))

    post_body = _macro_body(hooks_text, "NSIS_HOOK_POSTINSTALL")
    uninstall_body = _macro_body(hooks_text, "NSIS_HOOK_POSTUNINSTALL")
    checks.append((
        "LEGACY_CLEANUP_INSERTED_IN_BOTH_HOOKS",
        CLEANUP_INSERT in post_body and CLEANUP_INSERT in uninstall_body,
        f"{CLEANUP_INSERT} 必须同时出现在 NSIS_HOOK_POSTINSTALL（新装+升级）"
        "与 NSIS_HOOK_POSTUNINSTALL（卸载），少一处就有场景漏清理",
    ))

    # 清理必须排在 fail-closed Abort 之前：否则 launcher 供给失败中止安装时，
    # 陈旧任务不会被清掉。
    abort_match = ABORT_STMT_RE.search(post_body)
    cleanup_at = post_body.find(CLEANUP_INSERT)
    checks.append((
        "LEGACY_CLEANUP_PRECEDES_ABORT",
        abort_match is not None and cleanup_at >= 0 and cleanup_at < abort_match.start(),
        "POSTINSTALL 里清理必须早于 Abort（安装中止时陈旧任务仍应已被清除）",
    ))

    checks.append((
        "ABORT_STATEMENT_COUNT_UNCHANGED",
        len(ABORT_STMT_RE.findall(hooks_text)) == 1,
        "Abort 语句仍须恰为 1 条（清理是非致命的，不得新增 Abort）",
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
