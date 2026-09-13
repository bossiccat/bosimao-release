"""契约：来源 IP 收窄是可**显式关闭**的，且关闭时不许静默。

背景（2026-09-12）
----------------
媒体面从云端 Linux 容器回到用户各自的 Windows 机器后，「按来源 IP 收窄」在原理上
无法成立：每个用户出口 IP 都不同；且平台网关不消毒 `X-Forwarded-For`，来源本身可伪造。
此时真正的边界只剩共享密钥（网关断言 + 服务凭证）与 TLS pinning。

设计选择：用 `allowed_hosts` 里的 `*` 表示「不按来源收窄」，并在构造时打 WARNING。
—— 让这件事**显式且可审计**，好过填一个假网段（例如把平台边缘网段写进去）自我安慰。

注意：`*` 只关闭**来源**这一维；断言哈希与 certificate_binding 仍必须齐备（由既有测试覆盖）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/ → 使 `app` 可导入

from app.voice.trusted_gateway import TrustedGatewayIdentityMiddleware  # noqa: E402


def _middleware(allowed_hosts):
    return TrustedGatewayIdentityMiddleware(
        None,                    # 只测来源判定，不跑 ASGI 链
        gateway_assertion_hash="jax-static-v1$deadbeef",
        certificate_binding="binding",
        allowed_hosts=allowed_hosts,
    )


def test_default_rejects_arbitrary_source():
    """默认（loopback）下，任意公网来源不得被视为受信。"""
    mw = _middleware(["127.0.0.1", "::1"])
    assert mw._source_trusted("127.0.0.1") is True
    assert mw._source_trusted("203.0.113.9") is False
    assert mw._source_trusted("") is False


def test_cidr_is_supported():
    mw = _middleware(["10.15.254.0/24"])
    assert mw._source_trusted("10.15.254.166") is True
    assert mw._source_trusted("10.15.255.1") is False


def test_asterisk_disables_narrowing_for_any_source():
    """`*` = 显式声明不按来源收窄（用户机器出口 IP 各不相同，白名单无意义）。"""
    mw = _middleware(["*"])
    for host in ("203.0.113.9", "10.15.254.166", "2001:db8::1"):
        assert mw._source_trusted(host) is True


def test_asterisk_is_loud_not_silent(caplog):
    """关闭来源收窄必须**可审计**：构造时打 WARNING，绝不静默放宽。"""
    with caplog.at_level(logging.WARNING):
        _middleware(["*"])
    assert any("source-IP narrowing is DISABLED" in r.message % r.args
               if r.args else "source-IP narrowing is DISABLED" in r.message
               for r in caplog.records), "缺少关闭来源收窄的告警"


def test_asterisk_absent_keeps_narrowing_on():
    """未配置 `*` 时行为完全不变（默认安全）。"""
    mw = _middleware(["127.0.0.1", "::1", "10.15.254.0/24"])
    assert mw._source_trusted("198.51.100.7") is False
    assert mw._source_trusted("10.15.254.9") is True
