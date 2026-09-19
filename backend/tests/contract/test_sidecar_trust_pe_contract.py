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

# 用例数下限：当前 47。留余量，但足以在"套件被清空/被整体跳过"时变红。
_MIN_CASES = 45

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
        f"用例数 {len(cases)} < {_MIN_CASES}：套件被裁剪或整体未执行\n{detail}"
    )

    missing = [name for name in _REQUIRED_CASES if name not in names]
    assert not missing, (
        "关键用例缺失（文件还在但牙齿被拔）：\n  - "
        + "\n  - ".join(missing)
        + f"\n{detail}"
    )
