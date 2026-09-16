"""契约：控制面超时的**三处默认值必须一致**，且不得小到在公网 mTLS 下必然超时。

为什么单独立这条（2026-09-16 实测事故）
---------------------------------------
同一个"控制面超时"在本仓有过**三套互不相同的默认值**，而**生效的那一套最陈旧**：

| 位置 | 值 | 是否生效 |
|---|---|---|
| `redemption.py` 构造函数默认 | 5.0 / 15.0 | ❌ 死代码 |
| `config.py` 字段默认 | 0.5 / 2.0 | ✅ **线上实际用这套** |
| `ack_reporter.py` 构造函数默认 | 0.5 / 2.0 | ❌ 死代码 |

两个消费点**都显式传参**（`server.py` 的 `_redemption`、`drain_ack.py` 的 `build_ack_reporter`），
所以构造函数默认值永不生效 —— 有人以为"已把超时从 0.5/2.0 修正为 5/15"，
实际那笔修复落在了死代码上。

**为什么长期没被发现**：本地 `.env` 把两者覆盖成 2.0/10.0 ⇒ 本地永远正常；
部署 env 未设这两项 ⇒ 线上回落到 `config.py` 的 0.5/2.0；
而 `ack_reporter.py` 与 `config.py` **数值恰好相同** ⇒ **单读任意一处都"看起来自洽"**，
只有三处横向对读才会暴露。

后果（按消费点分开）：
- 兑付：失败是 **fail-closed 且终局**（`server.py` 发 `ctrl exit hello_redemption_failed`、永不建会话）
  ⇒ 用户侧"随机连不上"。
- drain 上报：超时即失败，而 fire-once 语义**没有重试机会** ⇒ 弱网下下行观测**静默丢失**。

本契约钉两件事：① 三处默认值必须相等（防再次漂移）；② 不得小于公网 mTLS 的最低合理值。
用 `inspect.signature` 与 `dataclasses.fields` 读默认值，**不实例化**（避免触发 env 覆盖等副作用）。
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from rtc_bridge.ack_reporter import AckReporterClient
from rtc_bridge.config import BridgeConfig
from rtc_bridge.redemption import HelloRedemptionClient

# 公网 HTTPS + mTLS 下的最低合理值。0.5s 连 TCP 都不够（TLS 握手本身常 >0.5s），
# 2.0s 覆盖不了控制面冷启动 + nonce 落库 ⇒ 稳态偶发失败。
MIN_CONNECT_S = 3.0
MIN_TOTAL_S = 10.0


def _config_default(field_name: str) -> float:
    for f in dataclasses.fields(BridgeConfig):
        if f.name == field_name:
            assert isinstance(f.default, (int, float)), f"{field_name} 没有字面量默认值"
            return float(f.default)
    raise AssertionError(f"BridgeConfig 里没有字段 {field_name}")


def _signature_default(cls: type, param: str) -> float:
    default = inspect.signature(cls.__init__).parameters[param].default
    assert isinstance(default, (int, float)), f"{cls.__name__}.{param} 的默认值不是数字：{default!r}"
    return float(default)


@pytest.mark.parametrize(
    ("config_field", "param", "label"),
    [
        ("control_plane_connect_timeout_s", "connect_timeout_s", "连接"),
        ("control_plane_total_timeout_s", "total_timeout_s", "总"),
    ],
)
def test_three_default_sets_agree(config_field: str, param: str, label: str) -> None:
    """三处默认值必须相等 —— 单读任意一处都自洽，正是这个缺陷能长期藏住的原因。"""
    values = {
        "config.py（线上实际生效）": _config_default(config_field),
        "redemption.py": _signature_default(HelloRedemptionClient, param),
        "ack_reporter.py": _signature_default(AckReporterClient, param),
    }
    assert len(set(values.values())) == 1, (
        f"{label}超时默认值三处不一致：{values}\n"
        "注意：生效的是 config.py 那套（两个消费点都显式传参），所以只改构造函数默认值是无效修复。"
    )


def test_defaults_are_not_too_small_for_public_mtls() -> None:
    """不得小到在公网 mTLS 下必然超时。

    这不是"越大约好"的偏好：取值过小会让兑付 fail-closed 拒绝建会话（终局失败）、
    并让 drain 上报静默丢失。要下调请先给出实测 RTT 与冷启动数据。
    """
    connect = _config_default("control_plane_connect_timeout_s")
    total = _config_default("control_plane_total_timeout_s")
    assert connect >= MIN_CONNECT_S, f"连接超时 {connect}s 低于公网 mTLS 的最低合理值 {MIN_CONNECT_S}s"
    assert total >= MIN_TOTAL_S, f"总超时 {total}s 低于最低合理值 {MIN_TOTAL_S}s"
    assert total > connect, f"总超时 {total}s 必须大于连接超时 {connect}s，否则连接预算没有意义"
