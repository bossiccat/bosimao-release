"""`DEFAULT_DOWN_*` 与 `BridgeConfig` 下行字段的一致性（保真性检查）。

⚠️ 这是**保真性检查，不是产品缺陷** —— 三份值现在是一致的，本文件是防止它们漂移。

为什么需要它
------------
下行队列预算的同一组数值在本仓存在**三份字面量副本**：

  · `shaper.py:25-27`  `DEFAULT_DOWN_MAX_FRAMES / _BYTES / _FRAME_AGE_MS`
                       —— 被 `DownlinkShaper.__init__` 的参数默认值使用（shaper.py:39-41）
  · `session.py:47-49` 同名常量。**是独立字面量**：session.py 只从 shaper import 了
                       `DownlinkShaper` 类（session.py:25），并没有 import 这几个常量。
                       —— 被 `PeerVoiceSession.__init__` 的参数默认值使用（session.py:85-87）
  · `config.py`        `BridgeConfig` 的 `down_max_frames / down_max_bytes /
                       down_max_frame_age_ms`

**生产路径由显式传参驱动**：`session.py:123-129` 给 `DownlinkShaper` 显式传
PeerVoiceSession 的参数；`server.py:221-223` 给 `PeerVoiceSession` 显式传
`self.cfg.down_*`。所以三份值不一致**不会**影响线上行为 —— 因此这不是产品缺陷。

但**测试**会走默认值。凡是不显式传预算的构造点，用到的就是某一份字面量副本：

  · 直接构造 `DownlinkShaper(send_frame=..., frame_ms=..., sample_rate=...)`（不传预算）
    → 走 **shaper 那份**：`test_downlink_frame_trace.py:105/127/149`、
      `test_rtc_bridge_server.py:552`
  · 直接构造 `PeerVoiceSession(...)`（不传预算）的用例 → 走 **session 那份**，
    该份已由 `backend/tests/contract/test_downlink_budget_contract.py:44-49` 守护

⇒ **shaper 那一份此前无人守**（`session.py:47` 的注释自称"契约测试守护"，实际守的是
session 那份）。它一旦与生产口径漂移，`test_downlink_frame_trace.py` 这类用例保真的
对象就不再是生产对象：它们会在自己那份预算下断言"没丢帧"，而线上跑的是另一组预算。
这类"绿得毫无意义"最难发现 —— 测试和断言都没错，只是测的不是生产配置。
（`test_downlink_queue_full_reply.py` 显式读 `BridgeConfig`，不受本问题影响。）

做法
----
用 `dataclasses.fields(BridgeConfig)` 读**类默认值**，不实例化 BridgeConfig
（实例化会读到 env 覆盖，测到的就不是"默认值"而是"本机配置"，在不同环境给出不同结论）。
逐字段断言相等，字段名用显式映射表而不是字符串拼接 —— 拼接会让"映射写错"和
"值真的不同"在断言上不可区分。另加两条：映射表必须覆盖 shaper 的全部 `DEFAULT_DOWN_*`
（防新增常量漏检）；三约束内部自洽（字节数 = 帧数 × 640B，帧龄 = 帧数 × 帧长）。
"""
from __future__ import annotations

import dataclasses

from rtc_bridge import shaper
from rtc_bridge.config import BridgeConfig

# 显式映射：shaper 侧常量名 → BridgeConfig 字段名。
# 不用字符串拼装（`DEFAULT_DOWN_` + field.upper()）——那会把"映射写错"变成静默空转。
_DOWNLINK_BUDGET_KEYS = {
    "DEFAULT_DOWN_MAX_FRAMES": "down_max_frames",
    "DEFAULT_DOWN_MAX_BYTES": "down_max_bytes",
    "DEFAULT_DOWN_MAX_FRAME_AGE_MS": "down_max_frame_age_ms",
}


def _class_default(field_name: str):
    """读 BridgeConfig 的**类默认值**（不实例化，避免 env 覆盖改变结论）。"""
    for f in dataclasses.fields(BridgeConfig):
        if f.name == field_name:
            # 每个字段都必须有显式默认值；default_factory 不是本处的形态。
            assert f.default is not dataclasses.MISSING, (
                f"BridgeConfig.{field_name} 不再有显式默认值 —— 本保真性检查的前提被破坏"
            )
            return f.default
    raise AssertionError(
        f"BridgeConfig 不再有字段 {field_name} —— 映射表过期，请同步本测试"
    )


def test_mapping_table_covers_every_default_down_constant():
    """反向：shaper 侧每一个 DEFAULT_DOWN_* 都必须在映射表里（防新增常量漏检）。"""
    present = sorted(n for n in dir(shaper) if n.startswith("DEFAULT_DOWN_"))
    assert present, "shaper 不再导出任何 DEFAULT_DOWN_* —— 常量被改名/删除，请同步本测试"
    assert present == sorted(_DOWNLINK_BUDGET_KEYS), (
        "shaper 的 DEFAULT_DOWN_* 集合变了，本保真性检查会漏测新常量："
        f"{present} vs {sorted(_DOWNLINK_BUDGET_KEYS)}"
    )


def test_downlink_defaults_match_bridge_config_defaults_one_by_one():
    """逐字段断言：shaper 的 DEFAULT_DOWN_* 必须与 BridgeConfig 的默认值相等。

    （保真性检查，不是产品缺陷 —— 生产走显式传参，见模块 docstring。）
    """
    for const_name, field_name in _DOWNLINK_BUDGET_KEYS.items():
        assert hasattr(shaper, const_name), f"shaper 缺少常量 {const_name}"
        shaper_value = getattr(shaper, const_name)
        config_value = _class_default(field_name)
        assert shaper_value == config_value, (
            f"{const_name}={shaper_value!r} 与 BridgeConfig.{field_name} 的默认值 "
            f"{config_value!r} 漂移。生产路径显式传 config，所以线上一律用 "
            f"{config_value!r}；而直接构造 DownlinkShaper（不传预算）的单元测试用的是 "
            f"{shaper_value!r} ⇒ 那些测试保真的对象已不是生产对象（绿得毫无意义）。"
            f"请把两份值改回一致，或让 shaper 直接从 config 取值。"
        )


def test_default_down_constants_are_consistent_with_frame_geometry():
    """常量内部自洽：字节预算必须恰好容得下帧数预算（640B/帧 @16k s16 20ms）。"""
    frame_bytes = int(BridgeConfig.sample_rate * 2 * (BridgeConfig.down_frame_ms / 1000))
    assert frame_bytes == 640, f"帧几何变了（{frame_bytes}B/帧），请同步本测试"
    assert shaper.DEFAULT_DOWN_MAX_BYTES == shaper.DEFAULT_DOWN_MAX_FRAMES * frame_bytes, (
        f"字节预算 {shaper.DEFAULT_DOWN_MAX_BYTES} 与帧数预算 "
        f"{shaper.DEFAULT_DOWN_MAX_FRAMES}×{frame_bytes} 不自洽 —— "
        f"字节约束会先于帧数约束触发，max_frames 形同虚设"
    )
    assert shaper.DEFAULT_DOWN_MAX_FRAME_AGE_MS == (
        shaper.DEFAULT_DOWN_MAX_FRAMES * BridgeConfig.down_frame_ms
    ), (
        "帧龄上限必须覆盖整段回复的排队时长（帧数 × 帧长）；否则「早到待播」的帧会被"
        "当陈旧数据按整帧切掉（2026-09-13 实测 6.88s 只收到 4.26s）"
    )
