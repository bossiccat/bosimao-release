"""契约：PE 信任判据必须是真的，且必须**真的被执行**。

为什么需要这座桥
----------------
`scripts/lib/sidecar-trust.js` 的 `isPeBinary` 位于**发布路径**上：它被
`assertProductionTrust` 用来校验 externalBin 与整个 `NATIVE_NAMES` 原生集，
而后者由 `scripts/build-sidecar-external-bin.js`（锁定检查 `sidecar-verify`）
在 selected immutable generation 内执行。

但它的守门测试住在 `scripts/test/**` —— 而**没有任何 workflow 收集那个目录**：
CI 里唯一的 node 测试调用是 deploy-cloudrun.yml 的 `cd sidecar && node --test
test/*.test.js`，只覆盖 `sidecar/test/**`。也就是说
`scripts/test/sidecar-package.test.js`（`assertProductionTrust` 的唯一测试）
从来没有在流水线里跑过，"本机跑过"是它此前唯一的证据。

本用例把那些 node 套件拉进 backend 契约套件 —— deploy-cloudrun.yml 会跑
`python -m pytest backend/tests/contract`，于是它们进入同一个门禁与同一份
junitxml 计数口径。先例：`test_barge_in_flush_contract.py` 用同样的方式
拉起 `sidecar/test/barge-in-flush-exec.test.js`。

不 skip：按仓库铁律，缺 node 时**响亮失败**而不是静默通过 ——
跳过一次等于让这条契约消失一次。

2026-09-19 追加：这些套件现在还承载**原生闭集的跨源不变式锁**。同一份"原生集 5 个
名字"在本仓有 5 份副本，其中 4 份在生产路径上且跨语言（JS 构建期 × 2、Rust 启动期 × 2），
此前互无锁。把锁放在这里（而不是 Rust 单元测试里）是因为它必须同时看到 JS 与 Rust 两侧，
而这条 pytest 用例已经是两端唯一的汇合点。

同一次修复还补了**跨语言 PE 判据锁**（JS 构建期 `isPeBinary` ↔ Rust 启动期
`is_pe_binary`）：Rust 侧此前只判 2 字节 `MZ`，而该文件头注释当时就声称"与
`scripts/lib/sidecar-trust.js` 同一策略"—— 一句没有锁背书的等价性声明。
注意 Rust 侧的行为牙齿（`MZ` + 40,000 字节填充必须判否、真实 172MB 产物必须放行）
住在 Rust 单元测试里，而**没有任何 workflow 跑 `cargo test`**，所以那些牙齿在 CI 上
不可见；这条 pytest 用例只能锁住两侧的判据形状与常量值，锁不住 Rust 的行为。

2026-09-20 追加：原生闭集由 5 件变 4 件 —— 媒体混流服务进程（CUI）被**刻意剪除**
（`scripts/lib/sidecar-trust.js` 的 `INTENTIONALLY_ABSENT_NATIVE`：名字 + 理由 + 移除日期 +
实测量）。这次改动同时给这座桥加了四条新用例：①它不在任何生产清单/派生副本里、且那份
"刻意缺席"记录必须带理由与日期（防"悄悄加回来"与"把记录删掉"两个方向）；②构建期剪除的
两向 fail-closed；③媒体家族 API 引用的构建期 preflight **含阳性对照**（构造一份含
`startMediaMixingServer(` 的样本并断言它真的被拦下），否则正则写错就是空集通过；
④buildPackage 里那两行调用**存在**（行为用例钉不住"调用被删"）。

同一次改动还**修正了一处漏算**：上面 2026-09-19 那段说"原生集 5 个名字有 5 份副本"，
漏掉了 `pet-ui/src-tauri/tests/support.rs` 的 fixture `NATIVE_NAMES`（集成测试副本）。
它不是生产清单，但它构造的 manifest 会被喂给 `validate_runtime`（`sidecar.rs` 的
`validate_for_launch` → `validate_native_subset` 的**精确集合相等**），而 `cargo`
没有任何 workflow 跑过 ⇒ 它是唯一一条能在 CI 上拦住"改了生产清单忘了改 fixture"的锁。
现已把这份副本与它的 `TRUST_VERSION` 一并纳入闭集锁/版本锁（第 6 份副本、
第 3 份策略版本副本），并补了对应变异。另外：那份 fixture 的 manifest 此前**没有**
`trust_version` 键，策略版本 bump 之后它会被判成"版本化之前的基线"，使整组集成测试
静默换成另一个错因 —— 已显式盖上当前策略版本。

2026-09-19 再追加：同族锁扩到**运行期可再生产物**（Chromium 在 generation 根写的
`debug.log`）。这条规则此前**只存在于 Rust 侧**（RP-07 起 3 处裸字面量），构建侧
（provenance 闭集 / 校验器 / 指针协议）一处都没有 ⇒ 两侧对"闭集"的定义分叉：
同一目录启动期放行、构建期判否。本机现役世代实测的形态就是"清单声明 70aa1d9b… /
实测 78cf15e5…"。锁同时要求两侧每个豁免点都**引用**共享常量而不是各写裸字面量。
"""
from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_NODE = shutil.which("node")

# 只有这两个：都确定性、不需要 electron/TRTC SDK、不需要 Windows。
# scripts/test/ 下其余文件涉及 crash worker / 迁移竞态，跑起来慢且与本契约无关。
_JS_SUITES = (
    ROOT / "scripts" / "test" / "sidecar-trust-pe.test.js",
    ROOT / "scripts" / "test" / "sidecar-package.test.js",
)

# 用例数下限：当前 59。留余量，但足以在"套件被清空/被整体跳过"时变红。
_MIN_CASES = 57

# 必须存在的用例名 —— 防"文件还在、牙齿被拔"（删掉关键用例但套件仍然全绿）。
_REQUIRED_CASES = (
    # 团队 2026-09-19 实测的确切假阳性形状：40,000 字节 0x41，只在开头放 MZ。
    "rejects the measured false positive: MZ plus 40,000 bytes of filler",
    # 生产可信门端到端：4MB 的 MZ-only blob 必须被拒（修复前它返回 'OK'）。
    "production trust rejects an oversized MZ-only externalBin",
    # 策略边界：CUI 仍是合法 PE，不得在 isPeBinary 里判否。
    "subsystem is not part of the PE-ness judgement (CUI is still a PE)",
    # 源码闭集与真实 sidecar/ 一致（漂移曾让 sidecar-verify 在 HEAD 上红了一周）。
    "APP_SOURCES matches the real sidecar/ top-level source set",
    # 原生闭集跨 5 份副本（JS×2 + Rust×2 + 冻结字面量）必须是同一集合。
    "the native closed set is one set across every production copy",
    # 启动期 Rust 的 PE 判据必须与构建期 JS 同口径 ——
    # `sidecar_runtime_trust.rs` 的头注释声称"同一策略"，而 2026-09-19 实测
    # 那句话一度是假的（Rust 侧只判 2 字节 MZ）。注释不算数，这条锁才算。
    "the Rust startup PE judgement is the same structural judgement as the JS one",
    # 运行期可再生产物（debug.log）必须同时被两侧豁免，且豁免点全部引用共享常量。
    "the runtime artifact exemption is one set across both languages",
    # 行为牙齿：已出厂世代（两侧都声明 debug.log）在运行期追加后仍必须通过校验。
    "a Chromium runtime artifact is not part of the payload closed set",
    # 行为牙齿：构建期必须把它从 staging 剪除，且两向 fail-closed。
    "the build prunes Chromium runtime artifacts out of staging instead of packing them",
    # 2026-09-20：刻意剪除的原生成员（媒体混流服务进程，CUI）。
    # 反向锁：它不在任何生产清单/派生副本里，且那份"刻意缺席"记录带名字/理由/日期/实测量。
    "the pruned native is a recorded decision and cannot come back silently",
    # 构建期剪除的两向 fail-closed（该在时不在 ⇒ 报错；剪完仍在 ⇒ 报错）。
    "the build prunes the intentionally absent native out of staging with two-way fail-closed",
    # 媒体家族 API 引用的构建期 preflight：**带阳性对照**，防"正则写错 ⇒ 空集通过"。
    "the media family API reference guard stops the build with a named error",
    "the media family guard neither misfires on real calls nor misses the real names",
    # 静态锁：buildPackage 里那两行调用必须存在（行为用例钉不住"调用被删"）。
    "the build path actually calls the pruned-native prune and the media family guard",
)


def test_node_is_available_for_the_trust_gate_suites() -> None:
    assert _NODE, (
        "未找到 node：CI 的 sidecar 门禁与 backend 契约套件在同一个 job 里跑，"
        "node 是硬前提；请安装 node 或修正 PATH（不跳过——跳过等于让这条契约消失）"
    )


def test_pe_trust_suites_run_green_and_keep_their_teeth(tmp_path: Path) -> None:
    node = _NODE or "node"
    for suite in _JS_SUITES:
        assert suite.exists(), f"守门套件被删了：{suite}"

    report = tmp_path / "node-trust.xml"
    proc = subprocess.run(
        [
            node, "--test",
            "--test-reporter=junit",
            f"--test-reporter-destination={report}",
            *[str(suite) for suite in _JS_SUITES],
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
    )

    assert report.exists(), (
        "node 没有产出 junit 报告，无法取数\n"
        f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    root = ET.parse(report).getroot()
    cases = list(root.iter("testcase"))
    failures = [c for c in cases if c.find("failure") is not None]
    errors = [c for c in cases if c.find("error") is not None]
    names = {c.get("name") for c in cases}

    detail = (
        f"rc={proc.returncode} tests={len(cases)} "
        f"failures={len(failures)} errors={len(errors)}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert proc.returncode == 0, f"PE 信任契约套件未通过\n{detail}"
    assert not failures and not errors, f"PE 信任契约套件有失败用例\n{detail}"
    assert len(cases) >= _MIN_CASES, (
        f"[junit 用例数低于下限] 本次只数到 {len(cases)} 个 <testcase>，"
        f"下限 _MIN_CASES={_MIN_CASES}。\n"
        "这**不是**随机/偶发失败，也不是 node 抖了一下 —— 只有下面两种成因，"
        "请对号入座，别靠重跑（重跑不会改变本次数到的用例数）：\n"
        "  1) 套件被裁剪/整体未执行：某个用例被删、被改名，或因条件而不执行；\n"
        "  2) 你**有意识**地减少/重组了用例，但忘了同步下调本文件里的 _MIN_CASES。\n"
        "处置：要么把用例补回来，要么**有意识**地下调 _MIN_CASES"
        "（并在提交信息里写清为什么少了几条）。\n"
        f"{detail}"
    )

    missing = [name for name in _REQUIRED_CASES if name not in names]
    assert not missing, (
        "关键用例缺失（文件还在但牙齿被拔）：\n  - "
        + "\n  - ".join(missing)
        + f"\n{detail}"
    )
