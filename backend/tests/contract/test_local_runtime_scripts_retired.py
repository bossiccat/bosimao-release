"""本地运行态脚本退役守卫。

用户政策：**不得用脚本解决问题；不得走本地；产品必须走云端。**

2026-09-11 完成第一轮退役：把"守护本机三件套"的自愈脚本 + 其计划任务注册脚本 +
adb 保活脚本、以及读取这些脚本的契约测试与为其服务的 Rust 包装器全部删除。
云端 `jax-voice-bridge` 的容器监督（子进程死即整体非零退出，由平台重启）取代了本地 watchdog。

本文件把"它们不许回来"变成可执行断言：只要有人重新引入这些路径，或者让其它文件
再依赖它们，测试立刻变红。
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

# 已退役：本地运行态 / 自愈 / 本机栈启动
RETIRED_SCRIPTS = (
    "scripts/jax-watchdog.ps1",
    "scripts/install-scheduled-tasks.ps1",
    "scripts/watchdog-check.ps1",
    "scripts/watchdog-sidecar.ps1",
    "scripts/jax-services.ps1",
    "scripts/start-all.ps1",
    "scripts/start-model.ps1",
    "scripts/start-relay.ps1",
    "scripts/lib-common.ps1",
    "scripts/dev.ps1",
    "scripts/adb-keepalive.bat",
    "scripts/adb-setup-phone.bat",
)

# 已退役脚本的专属测试与配套工具（随脚本一起删除）
RETIRED_TESTS = (
    "scripts/test/jax-watchdog-sidecar-ownership.test.js",
    "scripts/test/proxy-env-fold-contract.test.js",
    "scripts/test/relay-convergence-contract.test.js",
    "scripts/test/relay-singleton-contract.test.js",
    "scripts/test/rtc-bridge-stop-convergence-contract.test.js",
    "scripts/test/sidecar-credential-provision-orchestration.test.js",
    "scripts/test/relay-commandline-regex-contract.test.js",
    "backend/tests/contract/test_jax_services_exit_code_contract.py",
    "backend/tests/contract/test_rtc_bridge_service_bootstrap_contract.py",
    "backend/tests/contract/test_backend_single_instance_contract.py",
    "backend/tests/contract/test_loadenv_utf8_no_mojibake_contract.py",
    "backend/tests/contract/test_rtc_bridge_loadenv_reuse_contract.py",
    "tools/jax-watchdog-wrap/Cargo.toml",
    "tools/jax-watchdog-wrap/src/lib.rs",
    "tools/jax-watchdog-wrap/src/main.rs",
)

# 有意保留的引用，逐条说明理由：
#   - test_cloudbridge_service_contract.py：把这两个名字列为 bridge 服务**禁止出现**的
#     token（语义与"依赖"相反，它本身就是防线）。
#   - test_o018_installer_contract.py：docstring 记录历史方案沿革，非运行依赖。
ALLOWED_REFERENCES = {
    "backend/tests/contract/test_cloudbridge_service_contract.py",
    "backend/tests/contract/test_o018_installer_contract.py",
    # 本文件本身：必须写出这些名字才能守它们（自指豁免）
    "backend/tests/contract/test_local_runtime_scripts_retired.py",
}


def test_retired_scripts_stay_deleted() -> None:
    for relative_path in RETIRED_SCRIPTS + RETIRED_TESTS:
        assert not (ROOT / relative_path).exists(), (
            f"{relative_path} 属于已退役的本地运行态脚本/工具，不得重新引入；"
            "如确需恢复请先改本契约并说明理由"
        )


def test_retirement_record_exists() -> None:
    # 放在受跟踪的 docs/ 下（outputs/ 被 gitignore，CI 检出后不会有该文件）
    record = ROOT / "docs" / "retirements" / "local-runtime-scripts-2026-09-11.md"
    assert record.is_file(), "退役必须有记录（为什么、保留什么、机器侧残留、如何回滚）"
    text = record.read_text(encoding="utf-8")
    for section in ("退役清单", "保留项", "机器侧残留", "回滚"):
        assert section in text, f"退役记录缺少「{section}」"


def test_no_tracked_file_depends_on_retired_scripts() -> None:
    """仓库里不得再有人引用退役脚本（否则等于把它们变相复活）。

    只检查代码/配置类文件；*.md 一律跳过——退役记录、README、历史状态文档
    本来就需要提到这些名字。
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    names = [Path(p).name for p in RETIRED_SCRIPTS]
    offenders: dict[str, list[str]] = {}
    for relative_path in tracked:
        if relative_path in ALLOWED_REFERENCES or relative_path.endswith(".md"):
            continue
        candidate = ROOT / relative_path
        if not candidate.is_file() or candidate.stat().st_size > 2_000_000:
            continue
        try:
            body = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = [name for name in names if name in body]
        if hits:
            offenders[relative_path] = hits

    assert not offenders, f"仍有文件引用已退役脚本: {offenders}"
