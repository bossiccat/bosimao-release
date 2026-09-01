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

import pytest

from rtc_bridge.config import BridgeConfig
from rtc_bridge.drain_ack import build_ack_reporter
from rtc_bridge.redemption import HelloRedemptionClient
from rtc_bridge.server import BridgeServer
from rtc_bridge.tls_paths import REPO_CERTS_DIR, resolve_tls_file

# 本模块其余用例会产生大量预期内的 WARNING（ssl/httpx 降级），需要压制。
#
# 注意：`logging.disable()` 是**进程全局**的，若在模块顶层直接调用，会在
# 收集阶段就对整个 pytest 会话生效，压制其它测试模块 caplog 想抓的 WARNING
# 记录（本仓库已有 tests/unit/test_voice_auth.py 与
# tests/integration/test_voice_security_stream_routes.py 依赖 caplog）。
# 因此这里改用模块级 autouse fixture，保证本模块结束后恢复原状，不外溢。
@pytest.fixture(autouse=True, scope="module")
def _quiet_logs():
    logging.disable(logging.WARNING)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


@pytest.fixture
def logging_enabled():
    """临时解除日志压制，供日志断言用例使用（否则 caplog 抓不到记录）。"""
    logging.disable(logging.NOTSET)
    try:
        yield
    finally:
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


# ---------- 兜底可观测性：静默降级是本次 P1 的病根 ----------

def test_resolve_tls_file_warns_when_configured_candidates_all_unusable(
    logging_enabled, caplog,
):
    """配置了但全部失效 → 兜底 → 必须 WARNING。

    这是异常信号：env/cfg 给了值，却一个都不存在（典型即 .env 注入被 GBK
    误解码成乱码）。静默兜底会让运维以为配置生效，是本次 P1 难定位的根因。
    日志须同时给出「哪个候选不可用」与「实际用了哪个兜底文件」。
    """
    bad = rf"{_MOJIBAKE_DIR}\ca.crt"
    with caplog.at_level(logging.WARNING, logger="rtc_bridge.tls_paths"):
        resolved = resolve_tls_file("ca.crt", bad)

    assert resolved == str(REPO_CERTS_DIR / "ca.crt")
    warnings = [
        r for r in caplog.records
        if r.name == "rtc_bridge.tls_paths" and r.levelno >= logging.WARNING
    ]
    assert warnings, "兜底触发时必须打 WARNING，不得静默降级"
    message = warnings[-1].getMessage()
    assert bad in message, "日志须指明哪个候选路径不可用"
    assert str(REPO_CERTS_DIR / "ca.crt") in message, "日志须指明实际采用的兜底文件"


def test_resolve_tls_file_silent_when_no_candidate_configured(
    logging_enabled, caplog,
):
    """未配置（候选为空）→ 走仓库默认，是正常路径，**不得**告警。

    否则每次启动都刷 WARNING，告警疲劳等于没有告警。
    """
    with caplog.at_level(logging.WARNING, logger="rtc_bridge.tls_paths"):
        resolved = resolve_tls_file("ca.crt")

    assert resolved == str(REPO_CERTS_DIR / "ca.crt")
    warnings = [
        r for r in caplog.records
        if r.name == "rtc_bridge.tls_paths" and r.levelno >= logging.WARNING
    ]
    assert not warnings, "未配置时用仓库默认属正常路径，不应产生告警"


def test_resolve_tls_file_silent_when_candidate_blank(
    logging_enabled, caplog,
):
    """候选是空串/空白（env 设了但为空）视为未配置，同样不告警。"""
    with caplog.at_level(logging.WARNING, logger="rtc_bridge.tls_paths"):
        resolved = resolve_tls_file("ca.crt", "", "   ")

    assert resolved == str(REPO_CERTS_DIR / "ca.crt")
    warnings = [
        r for r in caplog.records
        if r.name == "rtc_bridge.tls_paths" and r.levelno >= logging.WARNING
    ]
    assert not warnings, "空白候选等同未配置，不应产生告警"


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


def test_resolve_tls_file_silent_when_config_equals_repo_default(
    logging_enabled, caplog,
):
    """配置值本身就指向仓库默认时，属原样命中，**不得**告警。

    这是今天的实际形态（.env 直接指向 <repo>/certs/）。若此处误报，每次启动
    都会刷 WARNING，告警疲劳等于没有告警。
    """
    fallback = str(REPO_CERTS_DIR / "ca.crt")
    with caplog.at_level(logging.WARNING, logger="rtc_bridge.tls_paths"):
        resolved = resolve_tls_file("ca.crt", fallback)

    assert resolved == fallback
    warnings = [
        r for r in caplog.records
        if r.name == "rtc_bridge.tls_paths" and r.levelno >= logging.WARNING
    ]
    assert not warnings, "配置值即兜底路径时属正常命中，不应产生告警"


def test_resolve_tls_file_warns_when_repo_default_also_missing(
    logging_enabled, caplog,
):
    """候选失效且仓库默认也缺失 → 仍要告警（并返回空串，绝不返回不存在的路径）。"""
    with caplog.at_level(logging.WARNING, logger="rtc_bridge.tls_paths"):
        resolved = resolve_tls_file("no-such-cert-xyz.crt", rf"{_MOJIBAKE_DIR}\x.crt")

    assert resolved == "", "兜底文件也不存在时必须返回空串，不得返回不存在的路径"
    warnings = [
        r for r in caplog.records
        if r.name == "rtc_bridge.tls_paths" and r.levelno >= logging.WARNING
    ]
    assert warnings, "配置失效且兜底也缺失时必须告警"


# ---------- 降级日志不得撒谎（误导性可观测性比没有日志更糟） ----------

def test_brain_degraded_log_must_not_claim_verification_disabled(
    monkeypatch, logging_enabled, caplog,
):
    """ca 不可得时的降级日志，不得谎称「校验已关闭」。

    实测依据：`urlopen(req, context=None)` 时，`http.client.HTTPSConnection`
    会自建 `ssl._create_default_https_context()` —— 走**系统默认信任库**，
    校验仍然开启，并按默认策略校验主机名。这与原文案
    "TLS verification disabled" 恰好相反；真出事故时会把 on-call 往
    「校验被关了」的错误方向带（误导性可观测性比没有日志更糟）。

    期望：告警仍需存在（确实是降级），但文案须准确描述实际行为。
    """
    from rtc_bridge import main as brain_main

    monkeypatch.delenv("BRAIN_CA_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    # 让仓库兜底也不可得，逼出 ca_file == "" 的降级分支
    monkeypatch.setattr(brain_main, "resolve_tls_file", lambda *a, **kw: "")

    with caplog.at_level(logging.WARNING, logger="rtc_bridge.main"):
        brain_main._make_brain_callback("https://127.0.0.1:8000/api/v1/brain")

    records = [
        r for r in caplog.records
        if r.name == "rtc_bridge.main" and r.levelno >= logging.WARNING
    ]
    assert records, "ca 不可得时必须告警（降级仍需可见）"
    message = records[-1].getMessage().lower()
    assert "verification disabled" not in message, (
        "校验并未关闭——context=None 会走系统默认信任库；"
        "此文案与事实相反，会误导事故排查"
    )
    assert "system" in message or "默认" in message or "default" in message, (
        "文案须准确说明实际行为：回退到系统默认信任库（校验仍开启）"
    )


def test_brain_unusable_ca_log_must_not_claim_verification_disabled(
    monkeypatch, logging_enabled, caplog, tmp_path,
):
    """CA 文件存在但不可用时，ssl_context 为 None，同样不得谎称校验已关闭。"""
    from rtc_bridge import main as brain_main

    bad_ca = tmp_path / "broken-ca.crt"
    bad_ca.write_text("not a pem", encoding="utf-8")
    monkeypatch.setenv("BRAIN_CA_FILE", str(bad_ca))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    with caplog.at_level(logging.WARNING, logger="rtc_bridge.main"):
        brain_main._make_brain_callback("https://127.0.0.1:8000/api/v1/brain")

    records = [
        r for r in caplog.records
        if r.name == "rtc_bridge.main" and r.levelno >= logging.WARNING
    ]
    assert records, "CA 不可用时必须告警"
    message = records[-1].getMessage().lower()
    assert "verification disabled" not in message, (
        "context=None 走系统默认信任库，校验未被关闭"
    )
