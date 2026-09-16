"""契约：jax-voice-bridge 镜像里的 sidecar TRTC SDK 版本必须停在"Linux 有原生 addon"的版本上。

背景（2026-09-16 事故，线上发布阻断）
------------------------------------
线上镜像的 sidecar 渲染进程在**模块加载期就死了**：`sidecar/rtc.js:29-30` 顶层
`require('trtc-electron-sdk')`，而镜像里缺原生 addon ⇒ 抛错 ⇒ 轮询从未启动、stdout 一个字都没有
（`/status.sidecar.output_tail` 为空）。手机侧表现为房间 `user size` 恒为 1——云端媒体面从未进房。

根因不是"下载偶尔失败"，而是**版本 pin 本身就无解**：
`trtc-electron-sdk` 的原生 addon 不在 npm 包里，由 `install`→`scripts/download.js` 按
`https://{domain}/trtc/electron/download/trtc-electron-sdk/{version}/trtc-electron-sdk-{osLabel}-{version}.zip`
下载（模板见 sdk 自身 `scripts/download.js:61`，平台标签见 `scripts/constant.js`）。
2026-09-16 实测，选版本要过**两道**筛：

* 筛 1「zip 存在」：111 个 12.x/13.x 版本里，`linux-x64` 只有 24 个；
  **任何 13.x 都没有 linux-x64 产物**（13.3.801 / 13.4.802-beta.3 实测 linux=404、win64=200）。
  于是 DeployId 014 的构建在下载阶段就 404 失败。
* 筛 2「zip 里真有 Node 原生绑定」：那 24 个里**只有 12 个**含
  `build/Release/trtc_electron_sdk.node`（`liteav/trtc.js:41` 的 require 目标）。
  断点精确落在 12.5.705-beta.0（含）与 12.6.706-beta.0（不含）之间 ——
  **12.7.706 也不含**，它 8.28 MB 的包里只有三个 `.so`。单看"HTTP 200 且体积够大"会选中它，
  然后在模块加载期抛 `Cannot find module`，与本次事故同一个死法。

所以容器侧只能停在 **12.5.705-beta.0**（可用集合里最新的一个）；
桌面端（Windows）`sidecar/package.json` 的 13.4.802-beta.3 是 win64，产物存在、不能照搬。

本文件是**结构性契约（仓库形状 + 常量）**，不是行为断言
------------------------------------------------------
为什么只能是静态断言：判定"某版本在 Linux 上有 addon"需要访问公网 CDN，测试里发网络请求
会让 CI 依赖外部可用性、变慢且不确定（且失败原因无法区分"CDN 抖了"和"版本真的没有"）。
所以这里把**已实测的版本集合**固化成 `cloudbridge/trtc-electron-sdk-linux-versions.json`
（含 `verified_at` / `method` / `negative_control` 证据字段，可人工复核重跑），
契约测试只断言"仓库里这几处常量彼此自洽"——这是能在离线、确定性前提下守住的边界。
真正的行为验证在构建期：Dockerfile 的 fail-closed 段会在 addon 缺失/零字节/版本漂移时 `exit 1`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "cloudbridge" / "Dockerfile"
WHITELIST = ROOT / "cloudbridge" / "trtc-electron-sdk-linux-versions.json"
SIDECAR_PACKAGE_JSON = ROOT / "sidecar" / "package.json"

ARG_NAME = "TRTC_ELECTRON_SDK_VERSION"
# 桌面端 pin（Windows/win64），本契约**禁止**容器跟随它
DESKTOP_PIN = "13.4.802-beta.3"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _whitelist() -> dict:
    return json.loads(WHITELIST.read_text(encoding="utf-8"))


def _arg_default(text: str) -> str:
    match = re.search(rf"^ARG\s+{ARG_NAME}=(\S+)\s*$", text, re.M)
    assert match, f"Dockerfile 必须声明 `ARG {ARG_NAME}=<version>`（版本 pin 的唯一入口）"
    return match.group(1)


# --- 1. ARG 默认值必须落在"Linux 有产物"的白名单里 ---------------------------


def test_arg_default_is_in_verified_linux_whitelist() -> None:
    version = _arg_default(_dockerfile())
    versions = _whitelist()["versions"]
    assert version in versions, (
        f"ARG {ARG_NAME}={version} 不在 linux-x64 产物白名单里；"
        f"容器里 sidecar 会因缺原生 addon 在模块加载期死掉（2026-09-16 线上事故）"
    )


def test_arg_default_is_the_latest_usable_linux_version() -> None:
    """ARG 默认值必须就是"可用集合"里最新的那个——避免能构建却停在很旧的版本。"""
    whitelist = _whitelist()
    assert _arg_default(_dockerfile()) == whitelist["latest_usable"]


def test_arg_default_actually_contains_the_node_binding() -> None:
    """**最关键的一条**：pin 的版本，其 linux zip 里必须真有 trtc_electron_sdk.node。

    2026-09-16 的教训：只验"zip 存在"会选中 12.7.706，而它 8.28 MB 的包内只有三个 .so，
    没有 Node 原生绑定 —— `liteav/trtc.js:41` 的 require 会在模块加载期抛错，
    与本次线上事故同一个死法，只是触发点从"下载 404"变成"包内缺件"。
    """
    whitelist = _whitelist()
    version = _arg_default(_dockerfile())
    entry = whitelist["versions"][version]
    assert entry["has_node_binding"] is True, (
        f"ARG {ARG_NAME}={version} 的 linux-x64 包内不含 build/Release/trtc_electron_sdk.node；"
        f"镜像里的 sidecar 会在 require('trtc-electron-sdk') 时抛 Cannot find module"
    )
    assert entry["node_binding_bytes"] and entry["node_binding_bytes"] > 1024 * 1024
    assert version in whitelist["usable_versions"]
    assert version == whitelist["latest_usable"]


def test_whitelist_exposes_the_node_binding_criterion() -> None:
    """"zip 存在"≠"可用"这件事必须以字段形式留在白名单里，否则下次还会只按 200 去筛。"""
    whitelist = _whitelist()
    assert "trtc_electron_sdk.node" in whitelist["usable_criterion"]
    usable = whitelist["usable_versions"]
    with_node = [
        v for v, e in whitelist["versions"].items() if e.get("has_node_binding") is True
    ]
    assert sorted(with_node) == sorted(usable)
    # 24 个有 zip 的版本里只有 12 个真可用；12.7.706 在内证名单里（这正是它不可用的原因）
    assert len(whitelist["versions"]) == 24
    assert len(usable) == 12
    assert "12.7.706" in whitelist["versions"]
    assert whitelist["versions"]["12.7.706"]["has_node_binding"] is False
    assert whitelist["versions"]["12.7.706"]["artifacts"] == [
        "libliteavsdk.so",
        "libtxffmpeg.so",
        "libuser_sig_gen.so",
    ]


def test_whitelist_has_no_13x_linux_artifact() -> None:
    """关键事实：13.x 从未发布 linux-x64 产物。这条是"为什么不能跟桌面端同版本"的机器可读版本。"""
    whitelist = _whitelist()
    assert whitelist["platform"] == "linux-x64"
    major_thirteen = [v for v in whitelist["versions"] if v.startswith("13.")]
    assert major_thirteen == [], f"白名单里出现了 13.x（实测无 linux-x64 产物）: {major_thirteen}"
    assert "13" in whitelist["key_fact"]
    # 对照组证据：13.x 在 linux 上 404，而 win64 存在
    probes = whitelist["negative_control"]["probes"]
    assert probes[DESKTOP_PIN] == {"linux-x64": 404, "win64": 200}


def test_whitelist_records_how_it_was_verified() -> None:
    """白名单必须自带复核方法与时间——没有出处的"白名单"就是硬编码。"""
    whitelist = _whitelist()
    assert whitelist["verified_at"] == "2026-09-16"
    assert "HEAD" in whitelist["method"]
    assert whitelist["url_template"].startswith("https://")
    # 模板里的平台位是 {osLabel}，白名单的 platform 字段把它钉成 linux-x64
    assert "{osLabel}" in whitelist["url_template"]
    assert "scripts/constant.js" in whitelist["url_template_source"]
    assert len(whitelist["versions"]) == 24
    assert all(entry["http_status"] == 200 for entry in whitelist["versions"].values())
    assert all(entry["size_bytes"] > 1024 * 1024 for entry in whitelist["versions"].values())
    # 三阶段复核里"读包内清单"这一步必须留下痕迹，否则筛 2 就退化成口头结论
    assert "中央目录" in whitelist["verification"]["step2_payload_manifest"]
    assert "ELF" in whitelist["verification"]["step3_full_download_check"]
    for entry in whitelist["versions"].values():
        assert entry["artifacts"], "每个版本都必须记录真实产物清单"


def test_desktop_pin_stays_on_windows_artifact_and_is_not_copied_to_linux() -> None:
    """桌面端 package.json 的 13.4.802-beta.3 是 win64 产物，禁改；也禁被容器复用。"""
    desktop = json.loads(SIDECAR_PACKAGE_JSON.read_text(encoding="utf-8"))
    assert desktop["dependencies"]["trtc-electron-sdk"] == DESKTOP_PIN
    assert DESKTOP_PIN not in _arg_default(_dockerfile())
    assert DESKTOP_PIN not in _whitelist()["versions"]


# --- 2. ARG 必须真的被用于安装，不能另写一个字面量版本 ------------------------


def test_dockerfile_installs_the_arg_instead_of_a_second_literal_version() -> None:
    text = _dockerfile()
    assert f"trtc-electron-sdk@${{{ARG_NAME}}}" in text, (
        "sidecar 安装阶段必须用 `trtc-electron-sdk@${ARG}` 显式安装，"
        "否则 node_modules 里的 JS 包不会等于 ARG 指定的版本"
    )
    # 除 ARG 默认值那一行外，不允许再出现任何字面量版本号形式的 package@version 安装
    literal_installs = re.findall(r"trtc-electron-sdk@(\d[^\s\"']*)", text)
    assert literal_installs == [], (
        f"安装命令里出现了写死的版本号（应与 ARG 解耦）: {literal_installs}"
    )


def test_arg_is_declared_before_it_is_used() -> None:
    """ARG 必须在引用它的 RUN 之前声明，否则 shell 里展开成空串、pin 静默失效。"""
    text = _dockerfile()
    arg_line = re.search(rf"^ARG\s+{ARG_NAME}=", text, re.M)
    use_line = re.search(rf"trtc-electron-sdk@\$\{{{ARG_NAME}\}}", text)
    assert arg_line and use_line
    assert arg_line.start() < use_line.start()


def test_dockerfile_keeps_foreground_scripts_for_install_output() -> None:
    """install 脚本输出默认被 npm 吞掉——这正是"下载失败却构建成功"长期不可见的原因。"""
    assert _dockerfile().count("--foreground-scripts") >= 2


# --- 3. 版本一致性校验必须存在（否则 JS 包与 addon 会各自漂移）----------------


def test_dockerfile_asserts_installed_js_package_version_equals_arg() -> None:
    """结构性断言：Dockerfile 必须读 node_modules 里 SDK 的 package.json version 并与 ARG 比对。

    为什么这一条只能是静态的：真正的行为验证需要在构建容器里跑 npm（本机无 docker、
    且 CI 也不能在单测里跑完整镜像构建）。但"断言存在"这件事本身可以在仓库层面守住，
    而且它是 fail-closed 链上唯一能拦住"JS 包被换成 13.x、addon 还是 12.x"的关卡。
    构建期的真实行为由 `exit 1` 分支保证（见下一个用例）。
    """
    text = _dockerfile()
    assert "trtc-electron-sdk/package.json" in text, "必须读取已安装 SDK 的 package.json 取 version"
    assert "INSTALLED" in text and "EXPECTED" in text
    # 断言是"等于"的比较，而不是只打日志
    assert re.search(r'\[\s*"\$INSTALLED"\s*!=\s*"\$EXPECTED"\s*\]', text), (
        "必须显式比较已安装版本与期望版本（$INSTALLED != $EXPECTED）"
    )


def test_version_mismatch_is_fail_closed_not_a_warning() -> None:
    """版本不一致必须让构建失败，不许降级为警告——宁可构建不过，也不产出坏镜像。"""
    text = _dockerfile()
    mismatch_block = text.split('"$INSTALLED" != "$EXPECTED"', 1)[1][:400]
    assert "exit 1" in mismatch_block, "版本漂移分支必须 exit 1（fail-closed）"
    assert "FATAL" in mismatch_block


def test_addon_presence_check_rejects_zero_byte_file() -> None:
    """存在但 0 字节的 addon 同样是坏镜像：断言用的是 -s（非空），不是 -f（仅存在）。"""
    text = _dockerfile()
    assert "-s \"$ADDON\"" in text
    assert "! -s \"$ADDON\"" in text
    assert "sha256sum" in text, "必须把 addon 的 sha256 打进构建日志，便于与线上产物对账"
