"""契约：两个客户端必须指向**同一个**云端控制面，且不得指回已退役的旧网关。

背景（2026-09-13 实测，两个都会「静默连错」的缺陷）
----------------------------------------------------
1. 桌面端 `pet-ui/src-tauri/src/main.rs` 把 `--sign-url` 硬编码成 `jax-backend`；
   手机端 `VoiceConfig.DEFAULT_SESSION_BASE_URL` 是 ADR-012 时代的 CloudBase
   HTTP 访问服务默认域名。**两者指向不同、且都不指向现役控制面。**
2. 危险之处在于它**不会报错**：
   · `jax-backend` 至今仍在线（`/health` 返回 200，但载荷是
     `model_server/proc_name`，属 PC 时代旧控制面）⇒ **连得上 ≠ 连对了**；
   · 旧手机域名 `/health` 已是 404，但客户端只在真正调用业务接口时才暴露。
3. 现役控制面是 CloudRun 服务 `jax-voice-api`（流水线 `deploy-cloudrun.yml` 的 api:9000
   部署的就是它），实测 `POST {host}/api/v1/voice/session` → 422（路由在、body 校验失败），
   且本机端到端语音链路全部跑在这个域名上。

本契约钉死两件事：
  A. 两端解析出的控制面 **host 必须相同**（这正是当初漂移掉的那条不变量）；
  B. 不得再出现指向旧控制面的**可执行字面量**。
注释里出现旧主机名是允许的（那是在解释历史），只有真正的赋值/字面量才拦。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAIN_RS = ROOT / "pet-ui" / "src-tauri" / "src" / "main.rs"
VOICE_CONFIG_KT = (ROOT / "mobile-app" / "app" / "src" / "main" / "java" / "com"
                   / "jax" / "voice" / "config" / "VoiceConfig.kt")

# 客户端使用的**控制面 host**。域名是公开非密信息。
EXPECTED_HOST = "jax-voice-api-283963-7-1436773060.sh.run.tcloudbase.com"
# 已退役的网关（出现即成缺陷）
RETIRED_HOSTS = ("jax-backend-283963", "ap-shanghai.app.tcloudbase.com")

_HOST_RE = re.compile(r"https://([A-Za-z0-9.\-]+)")


def _hosts_in_code(text: str) -> list[str]:
    """只取**非注释行**里的 https:// host，避免把解释历史的注释当成缺陷。"""
    hosts: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        # Rust `//`、Kotlin `//`、Kotlin 块注释行首 `*`
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        hosts.extend(_HOST_RE.findall(line))
    return hosts


def test_desktop_and_mobile_agree_on_the_control_plane_host() -> None:
    """两端必须指同一个控制面 —— 当初就是这条不变量漂移掉了。"""
    desktop = _hosts_in_code(MAIN_RS.read_text(encoding="utf-8"))
    mobile = _hosts_in_code(VOICE_CONFIG_KT.read_text(encoding="utf-8"))
    assert desktop, "桌面端应能从非注释代码里解析出控制面 host"
    assert mobile, "手机端应能从非注释代码里解析出控制面 host"

    assert EXPECTED_HOST in desktop, f"桌面端控制面应为 {EXPECTED_HOST}，实测 {desktop}"
    assert EXPECTED_HOST in mobile, f"手机端控制面应为 {EXPECTED_HOST}，实测 {mobile}"
    assert set(desktop) == set(mobile), (
        "桌面端与手机端必须指向同一个控制面 host（同一产品、同一控制面）；"
        f"实测 desktop={sorted(set(desktop))} mobile={sorted(set(mobile))}"
    )


def test_no_client_points_at_the_retired_gateways() -> None:
    """不得存在指向旧网关的**可执行字面量**（注释里的历史说明不算）。"""
    for name, path in (("desktop", MAIN_RS), ("mobile", VOICE_CONFIG_KT)):
        hosts = _hosts_in_code(path.read_text(encoding="utf-8"))
        for retired in RETIRED_HOSTS:
            assert not any(retired in h for h in hosts), (
                f"{name} 仍指向已退役网关 {retired}；实测 {hosts}"
            )


def test_desktop_control_plane_is_build_time_overridable() -> None:
    """换控制面不该要求改源码重新发版 —— 必须能构建期覆盖。"""
    src = MAIN_RS.read_text(encoding="utf-8")
    assert "option_env!(\"JAX_CONTROL_PLANE_URL\")" in src, \
        "桌面端 --sign-url 必须可由构建期环境变量覆盖"
    assert "jax-backend-283963-7-1436773060.sh.run.tcloudbase.com\"" not in src, \
        "不得再出现硬编码的旧控制面 sign-url 字面量"


def test_mobile_keeps_a_user_override_path() -> None:
    """手机端必须保留设置页覆盖能力（出厂默认变更不能靠改常量覆盖已分发 APK）。"""
    src = VOICE_CONFIG_KT.read_text(encoding="utf-8")
    assert "customSessionBaseUrl" in src, "必须保留用户自定义控制面入口"
    assert "ifBlank { DEFAULT_SESSION_BASE_URL }" in src, \
        "未自定义时必须回落到出厂默认"
