"""jax-voice-bridge（云端无头 TRTC 对端）的结构与行为契约。

背景：sidecar + rtc_bridge 原本跑在用户 Windows 机器上，靠 `jax-watchdog.ps1` 与
计划任务维持存活，另有一堆 .ps1/.bat 本地脚本兜底。用户已要求消灭一切本地运行态，
把这两个进程原样搬进同一容器。本文件把搬迁后的关键性质变成可执行断言：

1. 构建输入只有受跟踪源码（白名单精确到 sidecar 的 *.js/*.html/*.json）；
2. 本机 node_modules（Windows 二进制）绝不进镜像；
3. 子进程退出即整体退出——用平台重启替代本地 watchdog，不写自愈脚本；
4. 状态由服务自身报告（/api/v1/voice/bridge/status），供部署门禁判定。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"

FORBIDDEN_LOCAL_TOKENS = (
    "adb reverse",
    "adb connect",
    "tailscale",
    "jax-watchdog",
    "jax-services.ps1",
    "install-scheduled-tasks",
    "schtasks",
)


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "jax_voice_bridge_supervisor", CLOUDBRIDGE / "supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 1. 构建输入只有受跟踪源码 ---------------------------------------------


def test_manifest_declares_tracked_source_and_forbids_local_node_modules() -> None:
    manifest = json.loads((CLOUDBRIDGE / "CANONICAL_SOURCE.json").read_text(encoding="utf-8"))
    assert manifest["service"] == "jax-voice-bridge"
    assert manifest["dockerfile"] == "cloudbridge/Dockerfile"
    assert manifest["entrypoint"] == "cloudbridge/supervisor.py"
    assert "sidecar/node_modules" in manifest["forbidden_paths"]
    for relative_path in manifest["required_paths"]:
        assert (ROOT / relative_path).exists(), relative_path


def test_dockerfile_copies_every_tree_the_service_needs() -> None:
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    for copied in ("backend/app", "backend/rtc_bridge", "sidecar", "config", "cloudbridge"):
        assert f"COPY {copied} " in text, f"镜像缺少 COPY {copied}"
    assert "xvfb" in text, "Electron 无头运行需要 xvfb 提供虚拟显示"
    assert "npm install" in text, "sidecar 依赖必须在镜像内安装（Linux 原生 addon）"


def test_dockerignore_allows_bridge_sources_and_excludes_windows_node_modules() -> None:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for allowed in (
        "!backend/rtc_bridge/**",
        "!cloudbridge/**",
        "!sidecar/*.js",
        "!sidecar/*.json",
    ):
        assert allowed in text, f"构建上下文缺少放行规则 {allowed}"
    assert "sidecar/node_modules" in text, "必须显式排除本机 node_modules"


def test_entrypoint_exposes_one_platform_port() -> None:
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 9200" in text
    assert 'CMD ["python", "cloudbridge/supervisor.py"]' in text


def test_dockerfile_installs_xauth_needed_by_xvfb_run() -> None:
    """实测：缺 xauth 时 `xvfb-run` 报 "xauth command not found"，sidecar 起不来。"""
    text = (CLOUDBRIDGE / "Dockerfile").read_text(encoding="utf-8")
    assert "xauth" in text


def test_requirements_cover_the_rtc_bridge_closure_including_numpy() -> None:
    """实测：rtc_bridge/session.py → app.voice.apm_bridge 在模块级 import numpy，
    漏装会让容器启动即 ModuleNotFoundError 并整体退出。"""
    text = (CLOUDBRIDGE / "requirements.txt").read_text(encoding="utf-8")
    for requirement in ("websockets", "httpx", "numpy", "psycopg"):
        assert requirement in text, f"bridge 依赖缺少 {requirement}"


# --- 2. 不得出现本地绕行手段 ----------------------------------------------


def test_no_local_bypass_mechanisms_anywhere_in_the_service() -> None:
    for name in ("supervisor.py", "Dockerfile", "requirements.txt"):
        lowered = (CLOUDBRIDGE / name).read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_LOCAL_TOKENS:
            assert token not in lowered, f"{name} 出现本地/绕行手段: {token}"


def test_service_reads_credentials_only_from_environment() -> None:
    """容器内不得读取 .env 文件——凭据一律由 CloudRun 环境变量注入。

    注意别用裸子串 ".env" 判定：`os.environ` 本身就包含它。
    """
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    assert "dotenv" not in text
    for quoted in ('".env"', "'.env'", "/.env", "\\.env"):
        assert quoted not in text, f"supervisor 不应引用本地凭据文件: {quoted}"


# --- 3. 子进程退出即整体退出（替代 watchdog）------------------------------


class _StubChild:
    def __init__(self, *, alive: bool = True, exit_code: int | None = None) -> None:
        self._alive = alive
        self._exit_code = exit_code
        self.starts = 1
        self.pid = 4242
        self.kwargs = {"extra": "stub"}

    def alive(self) -> bool:
        return self._alive

    def reap(self):
        return self._exit_code

    def describe(self) -> dict:
        return {"alive": self._alive, "pid": self.pid, "starts": self.starts,
                "exit_code": self._exit_code}

    def signal(self, _sig) -> None:
        self._alive = False


def test_watch_exits_nonzero_when_rtc_bridge_dies() -> None:
    """rtc_bridge 死掉必须整体退出——由平台重启，绝不本地自愈。"""
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=False, exit_code=1)
    sup.sidecar = _StubChild(alive=True)

    with pytest.raises(SystemExit) as excinfo:
        sup.watch()
    assert excinfo.value.code == 1


def test_watch_exits_nonzero_when_sidecar_dies() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.shutting_down = False
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=False, exit_code=137)

    with pytest.raises(SystemExit) as excinfo:
        sup.watch()
    assert excinfo.value.code == 1


# --- 4. 状态由服务自身报告 -------------------------------------------------


def test_status_reports_children_health_and_verdict() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = "https://example.invalid"
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    object.__setattr__(sup, "_probe_bridge_health", lambda: "ok")

    payload = sup.status()
    assert payload["service"] == "jax-voice-bridge"
    assert payload["ok"] is True
    assert set(payload) >= {
        "rtc_bridge", "sidecar", "rtc_bridge_health", "sign_url",
        "device_id", "trtc_sdk_version", "uptime_s", "sidecar_enabled",
    }


def test_status_is_not_ok_when_health_probe_fails() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    sup.bridge_health_url = "http://127.0.0.1:19093/health"
    sup.sign_url = ""
    sup.device_id = "jax-cloud-bridge"
    sup.sidecar_enabled = True
    sup.bridge = _StubChild(alive=True)
    sup.sidecar = _StubChild(alive=True)
    object.__setattr__(sup, "_probe_bridge_health", lambda: "URLError")

    assert sup.status()["ok"] is False


def test_sdk_version_comes_from_the_sidecar_manifest() -> None:
    module = _load_supervisor()
    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    version = sup.sdk_version()
    assert version, "必须能从 sidecar/package.json 读到 TRTC SDK 版本"
