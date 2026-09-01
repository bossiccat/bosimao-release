"""控制面 mTLS 凭据路径兜底（mojibake-proof）——P1 排雷回归。

背景（640726d 提交信息点名留下的地雷）：
`.env` 含 4 条中文绝对路径（SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_CA_FILE /
RTC_BRIDGE_CLIENT_CERT_FILE / RTC_BRIDGE_CLIENT_KEY_FILE）。PS5.1 的 Load-Env
按 GBK 解码 UTF-8 .env 时，非 ASCII 段被误解码，这些路径以 mojibake 形态进入
rtc_bridge 子进程 env，再经 `rtc_bridge/config.py:109-111` 原样落到 BridgeConfig。

`rtc_bridge/main.py` 的 Brain 回调已于 244552c 用 `_resolve_brain_ca_file()`
修好（env → 存在性校验 → 仓库相对 certs/ca.crt 兜底），但控制面 mTLS 客户端
（`ack_reporter` / `redemption`，经 `drain_ack.build_ack_reporter` 与
`BridgeServer._redemption` 惰性构建）直接吃 cfg 里的裸路径，无存在性校验、
无兜底：

- `build_ack_reporter`：凭据守卫 `all(getattr(...))` 认为乱码串非空即"齐全"，
  随后 ssl context 构建抛 FileNotFoundError，被 `except Exception` 吞掉 →
  **静默返回 None**，drain 上报整体失效（fail-safe 能力静默消失）。
- `BridgeServer._redemption`：同一异常**无 try/except 保护**，直接冒泡 →
  hello 兑付失败 → sidecar 连不上（fail-closed 硬中断）。

契约（与 `_resolve_brain_ca_file` 三段语义一致）：
env/cfg 候选优先 → 存在性校验 → 仓库相对 certs/<default> 兜底。

乱码码点由 env-channels-fix-x2 在 PS 5.1.26100 真机抓取（见常量注释）。
"""
from __future__ import annotations

import logging
import pathlib

from rtc_bridge.config import BridgeConfig
from rtc_bridge.drain_ack import build_ack_reporter
from rtc_bridge.redemption import HelloRedemptionClient
from rtc_bridge.server import BridgeServer
from rtc_bridge.tls_paths import REPO_CERTS_DIR, resolve_tls_file

logging.disable(logging.WARNING)

# PS 5.1 Load-Env 摘掉 -Encoding UTF8 的实测产物（env-channels-fix-x2 真机抓取）。
# UTF-8 字节 e7 9b 91 e8 a7 86 61 70 70（"监视app"）被 CP936 解码为：
#   鐩(U+9429) 戣(U+6223) U+E74B(PUA) app
# 注意：CP936 把无映射双字节落到 PUA，而非 U+FFFD；Python 的 'gbk' codec 行为
# 不同（产出 U+FFFD），不可用 Python codec 反推 PS 行为。
_MOJIBAKE_DIR = "C:\\Users\\Administrator\\WorkBuddy\\鐩戣\ue74bapp\\certs"


def _mojibake_cfg() -> BridgeConfig:
    """凭据齐全、但三条证书路径全是 GBK 乱码（磁盘上并不存在）。"""
    cfg = BridgeConfig()
    cfg.control_plane_base_url = "https://control-plane.example"
    cfg.control_plane_service_credential = "bridge-secret"  # 测试哑值，非真实密钥
    cfg.control_plane_gateway_assertion = "assertion-token"
    cfg.control_plane_ca_file = rf"{_MOJIBAKE_DIR}\ca.crt"
    cfg.control_plane_client_cert_file = rf"{_MOJIBAKE_DIR}\client.crt"
    cfg.control_plane_client_key_file = rf"{_MOJIBAKE_DIR}\client.key"
    return cfg


def test_mojibake_paths_indeed_do_not_exist():
    """前置断言：乱码路径在磁盘上确实不存在（否则本组测试无意义）。"""
    cfg = _mojibake_cfg()
    for value in (
        cfg.control_plane_ca_file,
        cfg.control_plane_client_cert_file,
        cfg.control_plane_client_key_file,
    ):
        assert not pathlib.Path(value).is_file(), value


# ---------- 共用解析器：三段语义 ----------

def test_resolve_tls_file_prefers_existing_candidate(tmp_path):
    """① 候选路径存在即优先采用（env/cfg 优先）。"""
    p = tmp_path / "explicit-ca.crt"
    p.write_text("dummy", encoding="utf-8")
    assert resolve_tls_file("ca.crt", str(p)) == str(p)


def test_resolve_tls_file_skips_missing_candidate_and_falls_back():
    """② 存在性校验失败 → ③ 回退仓库相对 certs/<default>。"""
    resolved = resolve_tls_file("ca.crt", rf"{_MOJIBAKE_DIR}\ca.crt")
    assert resolved == str(REPO_CERTS_DIR / "ca.crt"), (
        "候选路径失效时必须回退仓库相对 certs/ca.crt"
    )
    assert pathlib.Path(resolved).is_file(), "回退路径必须真实存在"


def test_resolve_tls_file_falls_back_for_client_credential_pair():
    """mTLS 客户端证书/私钥同样走兜底（三条路径缺一不可）。"""
    assert pathlib.Path(
        resolve_tls_file("client.crt", rf"{_MOJIBAKE_DIR}\client.crt")
    ).is_file()
    assert pathlib.Path(
        resolve_tls_file("client.key", rf"{_MOJIBAKE_DIR}\client.key")
    ).is_file()


def test_resolve_tls_file_ignores_empty_candidates():
    """空串/空白候选（env 未设置）不参与选择。"""
    assert resolve_tls_file("ca.crt", "", "   ") == str(REPO_CERTS_DIR / "ca.crt")


def test_resolve_tls_file_returns_empty_when_nothing_available():
    """全部候选（含仓库默认）都不可得 → 空串，交调用方显式降级/报错。"""
    assert resolve_tls_file("no-such-cert-xyz.crt", rf"{_MOJIBAKE_DIR}\x.crt") == ""


def test_brain_ca_resolver_reuses_shared_tls_paths(monkeypatch):
    """`_resolve_brain_ca_file` 三段语义保持不变（244552c 契约不回退）。"""
    from rtc_bridge import main as brain_main

    monkeypatch.setenv("BRAIN_CA_FILE", rf"{_MOJIBAKE_DIR}\ca.crt")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert brain_main._resolve_brain_ca_file() == str(REPO_CERTS_DIR / "ca.crt")

    existing = REPO_CERTS_DIR / "ca.crt"
    monkeypatch.setenv("BRAIN_CA_FILE", str(existing))
    assert brain_main._resolve_brain_ca_file() == str(existing)


# ---------- 地雷 1：ack_reporter 静默失效 ----------

def test_ack_reporter_still_built_when_cfg_paths_are_mojibake():
    """乱码路径不得让 ack 上报能力静默消失。"""
    client = build_ack_reporter(_mojibake_cfg())
    assert client is not None, (
        "cfg 证书路径为乱码时，ack reporter 必须靠仓库相对兜底正常构建，"
        "而不是被 except Exception 吞掉静默失效（drain 上报静默消失）"
    )


# ---------- 地雷 2：redemption 硬崩溃 ----------

def test_redemption_client_builds_when_cfg_paths_are_mojibake():
    """乱码路径不得让 hello 兑付客户端构建崩溃（惰性构建、无兜底）。"""
    bridge = BridgeServer(_mojibake_cfg(), {})
    client = bridge._redemption
    assert isinstance(client, HelloRedemptionClient)
