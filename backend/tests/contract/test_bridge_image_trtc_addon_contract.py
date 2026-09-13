"""契约：镜像构建必须对 TRTC 原生 addon 做 fail-closed 校验。

背景（2026-09-12 事故，云端实测）
--------------------------------
`jax-voice-bridge` 的镜像构建「成功」，但容器里 `trtc-electron-sdk` 的 Linux 原生库根本不存在。
渲染进程加载 `rtc.js` 时即抛：

    Cannot find module '../build/Release/trtc_electron_sdk.node'

→ `rtc.js` 一行都不执行 → 既不调 `/api/v1/voice/session`、也不写日志。
**sidecar 与手机模拟器用的是同一个 addon，所以云端媒体面从来没能进过 TRTC 房间**；
此前把「端到端不通」归因到 hello 兑付门禁/白名单，都不是第一因。

根因：该原生库不在 npm 包里，由 `install` 脚本从 web.sdk.qcloud.com 下载 zip 解压到
`build/Release/`。下载失败**被脚本静默吞掉**，于是构建照样通过，产出「能起但连不上 TRTC」
的镜像——又是同一个静默失败模式。

因此 Dockerfile 必须：① 缺失时显式重下有界次数；② 仍缺失就让构建失败。
本测试用静态断言把这条守住，防止以后有人「为了构建顺畅」把校验删掉。
"""
from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[3] / "cloudbridge" / "Dockerfile"
ADDON_PATH = "build/Release/trtc_electron_sdk.node"


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_asserts_trtc_native_addon_presence():
    text = _text()
    assert ADDON_PATH in text, "Dockerfile 必须校验原生 addon 路径"
    assert "exit 1" in text, "addon 缺失时必须以非零码结束构建（fail-closed）"


def test_dockerfile_retries_the_download_before_failing():
    text = _text()
    # 有界重试：网络抖动不该直接判死，但重试次数必须有界（不写死循环）
    assert "npm run download" in text, "必须显式触发下载脚本"
    assert "for attempt in" in text, "必须有有界重试循环"


def test_dockerfile_install_shows_script_output():
    """install 脚本输出默认被 npm 吞掉，这正让『下载失败』长期不可见。"""
    assert "--foreground-scripts" in _text()


def test_dockerfile_does_not_copy_host_node_modules():
    """本机 node_modules 是 Windows 二进制，绝不能进镜像。"""
    text = _text()
    assert "COPY sidecar ./sidecar" in text
    lower = text.lower()
    assert "copy sidecar/node_modules" not in lower
