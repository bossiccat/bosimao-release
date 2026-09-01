"""控制面 mTLS 凭据路径兜底（mojibake-proof）——P1 排雷回归。

背景（640726d 提交信息点名留下的地雷）：
`.env` 含 4 条中文绝对路径（SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_CA_FILE /
RTC_BRIDGE_CLIENT_CERT_FILE / RTC_BRIDGE_CLIENT_KEY_FILE）。PS5.1 的 Load-Env
按 GBK 解码 UTF-8 .env 时，非 ASCII 段被误解码（"监视app" → "鐩戣…"），这些
路径以 mojibake 形态进入 rtc_bridge 子进程 env，再经
`rtc_bridge/config.py:109-111` 原样落到 `BridgeConfig`。

`rtc_bridge/main.py` 的 Brain 回调已于 244552c 用 `_resolve_brain_ca_file()`
修好（env → 存在性校验 → 仓库相对 certs/ca.crt 兜底），但控制面 mTLS 客户端
（`ack_reporter` / `redemption`，经 `drain_ack.build_ack_reporter` 与
`BridgeServer._redemption` 惰性构建）直接吃 cfg 里的裸路径，无存在性校验、
无兜底：

- `build_ack_reporter`：`ssl.create_default_context(cafile=<乱码>)` 抛
  FileNotFoundError → 被 `except Exception` 吞掉 → **静默返回 None**，
  drain 上报整体失效（fail-safe 能力静默消失，最难排查的一种）。
- `BridgeServer._redemption`：同一异常**无 try/except 保护**，直接冒泡 →
  hello 兑付失败 → sidecar 连不上（fail-closed 硬中断）。

契约（与 `_resolve_brain_ca_file` 三段语义一致）：
cfg/env 候选优先 → 存在性校验 → 仓库相对 certs/<default> 兜底。
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

# PS5.1 Load-Env GBK 误解码实锤形态（rtc_bridge.log.err 2026-09-01）
_MOJIBAKE_DIR = r"C:\Users\Administrator\WorkBuddy\鐩戣…app\certs"


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


def test_ack_reporter_still_built_when_cfg_paths_are_mojibake():
    """乱码路径不得让 ack 上报能力静默消失。

    现状缺陷：`build_ack_reporter` 的 `all(getattr(...))` 守卫认为凭据齐全
    （乱码串非空），随后 ssl context 构建抛 FileNotFoundError，被
    `except Exception` 吞掉 → 返回 None → drain 上报全部跳过。
    """
    client = build_ack_reporter(_mojibake_cfg())
    assert client is not None, (
        "cfg 证书路径为乱码时，ack reporter 必须靠仓库相对兜底正常构建，"
        "而不是被 except Exception 吞掉静默失效（drain 上报静默消失）"
    )


def test_redemption_client_builds_when_cfg_paths_are_mojibake():
    """乱码路径不得让 hello 兑付客户端构建崩溃（惰性构建、无兜底）。"""
    bridge = BridgeServer(_mojibake_cfg(), {})
    client = bridge._redemption
    assert isinstance(client, HelloRedemptionClient)
