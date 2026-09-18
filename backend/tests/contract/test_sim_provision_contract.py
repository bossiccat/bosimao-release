"""云端 provisioning 与手机模拟器日志的契约。

背景：为了让「没有真机也能验证语音链路」这条能力可信，模拟器必须走**和真机完全一致**
的 provisioning 路径（owner 签 pairing_code → register 换 device 凭证 → 用它签 session）。
本文件把三件容易悄悄退化的事变成断言：

1. provisioning 的三步顺序、认证归属与 URL 段名（privacy 段名尤其容易写错）；
2. 任何阶段的失败都只暴露「阶段:错误码」，**绝不**泄露 owner 凭证 / pairing_code /
   credential_secret（凭证一旦进了日志或状态端点就等于泄露）；
3. **supervisor 不再接线**（2026-09-17）：容器内的手机模拟器已从产品运行期整体移除，
   supervisor 里不再有 provisioning 调用、sim 子进程与 `/status.simulation`。

另附 sim_phone 新日志行（签发失败带码 / 远端就绪毫秒 / 远端超时）的解析断言。

⚠️ 模块**没有**被删：`scripts/sim/run-phone.py` 仍在容器外用它做端到端验证，
   所以第 1、2 节的口径与第 4、5 节的解析契约必须一直有效。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[3]
CLOUDBRIDGE = ROOT / "cloudbridge"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, CLOUDBRIDGE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_sim_provision():
    return _load("jax_voice_bridge_sim_provision", "sim_provision.py")


def _load_sim_phone():
    return _load("jax_voice_bridge_sim_phone", "sim_phone.py")


def _load_supervisor():
    return _load("jax_voice_bridge_supervisor", "supervisor.py")


OWNER_SECRET = "owner-secret-do-not-leak"
PAIRING_SECRET = "pc-secret-abcdefghijklmnop"
CRED_SECRET = "cred-secret-do-not-leak"
DEVICE_ID = "11111111-2222-3333-4444-555555555555"


def _stub_request(sp, *, overrides: dict | None = None):
    """构造一个替身 `_request`：按 URL 后缀命中预设响应，记录每次调用。"""
    responses = {
        sp.PRIVACY_CLOUD_PROCESSING_PATH: (
            200, {"code": 0, "data": {"setting": "cloud_processing_enabled"}},
        ),
        sp.PAIRING_CODE_PATH: (
            200, {"code": 0, "data": {"pairing_code": PAIRING_SECRET,
                                      "expires_at": "2030-01-01T00:00:00Z"}},
        ),
        sp.REGISTER_PATH: (
            201, {"code": 0, "data": {"device_id": DEVICE_ID, "credential_id": "cred-id",
                                      "credential_secret": CRED_SECRET,
                                      "expires_at": "2030-01-01T00:00:00Z"}},
        ),
    }
    if overrides:
        responses.update(overrides)
    calls: list[dict] = []

    def fake_request(method, url, payload, *, owner_credential="", device_token="", timeout_s=20):
        calls.append({"method": method, "url": url, "payload": payload,
                      "owner": owner_credential, "device": device_token})
        for path, resp in responses.items():
            if url.endswith(path):
                return resp
        raise AssertionError(f"unexpected url: {url}")

    return fake_request, calls


# --- 1. nonce ---------------------------------------------------------------


def test_new_nonce_is_64_hex_and_unique() -> None:
    sp = _load_sim_provision()
    first, second = sp.new_nonce(), sp.new_nonce()
    assert re.fullmatch(r"[0-9a-f]{64}", first), first
    assert re.fullmatch(r"[0-9a-f]{64}", second), second
    assert first != second, "nonce 必须一次性、不可复用"


# --- 2. provisioning 成功路径：顺序 / 认证 / URL / token 形状 ---------------


def test_provision_sim_device_runs_privacy_pairing_register_in_order() -> None:
    sp = _load_sim_provision()
    fake_request, calls = _stub_request(sp)

    with mock.patch.object(sp, "_request", fake_request):
        device = sp.provision_sim_device(
            "https://cp.example/", OWNER_SECRET, device_name="jax-sim-phone"
        )

    assert [c["method"] for c in calls] == ["PATCH", "POST", "POST"]
    assert calls[0]["url"].endswith(sp.PRIVACY_CLOUD_PROCESSING_PATH)
    assert calls[1]["url"].endswith(sp.PAIRING_CODE_PATH)
    assert calls[2]["url"].endswith(sp.REGISTER_PATH)

    # privacy 门禁：URL 段名必须是 cloud_processing；body 必须是 {"enabled": true}
    assert calls[0]["payload"] == {"enabled": True}, "privacy body 形状必须与 SetPrivacyRequest 一致"
    assert calls[0]["owner"] == OWNER_SECRET

    # pairing-code 用 owner 认证；register 以 pairing_code 为 bootstrap，不带任何 Bearer
    assert calls[1]["owner"] == OWNER_SECRET
    assert calls[1]["payload"] == {"platform": "android", "device_name_hint": "jax-sim-phone"}
    assert calls[2]["owner"] == "" and calls[2]["device"] == "", "register 不得携带 owner/device 凭证"
    assert calls[2]["payload"]["pairing_code"] == PAIRING_SECRET

    assert device.device_id == DEVICE_ID
    assert device.credential_token == f"{DEVICE_ID}.{CRED_SECRET}"


def test_request_sets_json_and_nonce_headers_and_honours_owner_auth() -> None:
    sp = _load_sim_provision()
    captured = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b'{"code": 0}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Resp()

    with mock.patch.object(sp.urllib.request, "urlopen", fake_urlopen):
        status, body = sp._request(
            "POST", "https://cp.example/x", {"a": 1},
            owner_credential=OWNER_SECRET, timeout_s=7,
        )

    assert (status, body) == (200, {"code": 0})
    headers = {k.lower(): v for k, v in captured["request"].header_items()}
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == f"Bearer {OWNER_SECRET}"
    assert re.fullmatch(r"[0-9a-f]{64}", headers["x-request-nonce"])
    assert captured["timeout"] == 7


def test_request_surfaces_http_error_body_code_instead_of_raising() -> None:
    """HTTPError 也是响应：必须读出 body 里的业务码，供上层判码后只报「阶段:码」。"""
    sp = _load_sim_provision()
    err = urllib.error.HTTPError(
        "https://cp.example/x", 403, "Forbidden", {}, io.BytesIO(b'{"code": 40301}')
    )
    with mock.patch.object(sp.urllib.request, "urlopen", side_effect=err):
        status, body = sp._request("POST", "https://cp.example/x", {})
    assert status == 403 and body["code"] == 40301


# --- 3. 失败只报「阶段:码」，绝不泄露凭证 ---------------------------------


@pytest.mark.parametrize(
    "overrides,stage,expected_code",
    [
        ({"privacy": (200, {"code": 40301})}, "privacy", 40301),
        ({"pairing": (400, {"code": 40001})}, "pairing_code", 40001),
        ({"register": (409, {"code": 40901})}, "register", 40901),
        ({"register": (500, {})}, "register", 500),
    ],
)
def test_stage_failure_reports_stage_and_code_without_leaking_credentials(
    overrides, stage, expected_code
) -> None:
    sp = _load_sim_provision()
    key_map = {
        "privacy": sp.PRIVACY_CLOUD_PROCESSING_PATH,
        "pairing": sp.PAIRING_CODE_PATH,
        "register": sp.REGISTER_PATH,
    }
    resolved = {key_map[k]: v for k, v in overrides.items()}
    fake_request, _calls = _stub_request(sp, overrides=resolved)

    with mock.patch.object(sp, "_request", fake_request):
        with pytest.raises(sp.SimProvisionError) as excinfo:
            sp.provision_sim_device("https://cp.example", OWNER_SECRET)

    exc = excinfo.value
    assert exc.stage == stage
    assert str(exc) == f"{stage}:{expected_code}"

    text = str(exc)
    for secret in (OWNER_SECRET, PAIRING_SECRET, CRED_SECRET):
        assert secret not in text, "异常文本绝不能携带凭证"


# --- 4. resolve_sim_device 三态 --------------------------------------------


def test_resolve_prefers_explicit_token_and_issues_no_http() -> None:
    sp = _load_sim_provision()
    with mock.patch.object(sp, "_request", side_effect=AssertionError("不应发 HTTP")):
        with mock.patch.object(sp, "provision_sim_device",
                               side_effect=AssertionError("不应触发 provisioning")):
            device = sp.resolve_sim_device(
                base_url="https://cp.example", explicit_token=f"{DEVICE_ID}.{CRED_SECRET}"
            )
    assert device.device_id == DEVICE_ID
    assert device.credential_token == f"{DEVICE_ID}.{CRED_SECRET}"


def test_resolve_uses_owner_credential_when_no_explicit_token() -> None:
    sp = _load_sim_provision()
    sentinel = sp.SimDevice(DEVICE_ID, f"{DEVICE_ID}.{CRED_SECRET}", "2030-01-01T00:00:00Z")
    with mock.patch.object(sp, "provision_sim_device", return_value=sentinel) as prov:
        device = sp.resolve_sim_device(
            base_url="https://cp.example", owner_credential=OWNER_SECRET,
            device_name="jax-sim-phone",
        )
    assert device is sentinel
    prov.assert_called_once_with(
        "https://cp.example", OWNER_SECRET, device_name="jax-sim-phone", timeout_s=20
    )


def test_resolve_treats_malformed_explicit_token_as_absent() -> None:
    """非空但不成 `<id>.<secret>` 形状的 token 不可直接用，退回 owner 配对路径。"""
    sp = _load_sim_provision()
    sentinel = sp.SimDevice(DEVICE_ID, f"{DEVICE_ID}.{CRED_SECRET}", "")
    with mock.patch.object(sp, "provision_sim_device", return_value=sentinel) as prov:
        device = sp.resolve_sim_device(
            base_url="https://cp.example", explicit_token="no-dot-here",
            owner_credential=OWNER_SECRET,
        )
    assert device is sentinel
    prov.assert_called_once()


def test_resolve_without_any_credential_fails_config_stage() -> None:
    sp = _load_sim_provision()
    with pytest.raises(sp.SimProvisionError) as excinfo:
        sp.resolve_sim_device(base_url="https://cp.example")
    assert str(excinfo.value) == "config:SIM_DEVICE_CREDENTIAL_MISSING"


# --- 5. sim_phone 新日志行解析 --------------------------------------------


def test_phone_log_parses_sign_failure_code_remote_ready_and_timeout() -> None:
    """⚠️ 夹具必须是**真实落盘形态**：`sidecar/logger.js:19` 拼的是
    `[ISO时间] [scope] msg`，所以真机上这三行都带 `[PHONE] ` 前缀。

    2026-09-16 审计发现：本用例原本三条夹具全写成裸 `PHONE …`（**生产上不存在的形态**），
    于是它们在「正则要求裸 `PHONE `」这个缺陷下**照样通过** —— 有测试、看着覆盖了，
    实际是假绿：真机上 `remote_ready_ms` 恒为 null（实测 e2e-summary.json），
    而 `签到失败 code=` 解析不出业务码，`remote_not_ready` note 一条也不留。
    现在夹具换真实形态，正则必须真的认 `[PHONE] ` 才可能绿。
    """
    sim = _load_sim_phone()

    failed = sim.parse_phone_log(["[2026-09-16T00:44:20.298Z] [PHONE] 签发失败 code=-1002"])
    assert failed.state == "failed"
    assert failed.failure == "PHONE_SESSION_SIGN_FAILED:-1002"

    ready = sim.parse_phone_log(["[2026-09-16T00:44:23.152Z] [PHONE] 远端就绪 @180ms"])
    assert ready.remote_ready_ms == 180
    assert ready.to_dict()["remote_ready_ms"] == 180

    timeout = sim.parse_phone_log(
        ["[2026-09-16T00:44:23.152Z] [PHONE] 远端未就绪（5000ms 超时，继续上行）"]
    )
    assert "remote_not_ready" in timeout.notes

    # 字段稳定：未就绪也输出 remote_ready_ms=None，下游无需判 key 是否存在
    assert sim.parse_phone_log([]).to_dict()["remote_ready_ms"] is None


def test_phone_log_keeps_the_legacy_sign_failure_line() -> None:
    sim = _load_sim_phone()
    legacy = sim.parse_phone_log(["PHONE PHONE_SESSION_SIGN_FAILED"])
    assert legacy.state == "failed"
    assert legacy.failure == "PHONE_SESSION_SIGN_FAILED"


# --- 6. supervisor 接线：**已移除**（2026-09-17）-----------------------------
#
# 容器内的手机模拟器已从产品运行期整体移除：supervisor 不再调用 provisioning、
# 不再构造 sim 子进程、`/status` 不再有 simulation 段（移除本身的契约在
# `test_bridge_sim_phone_removed_contract.py`）。于是"supervisor 怎么接线"这一节
# 失去了对象，这里改成断言**反面**。
#
# 但原来那条安全口径**没有失效、也不许失效**：任何阶段的失败只暴露「阶段:码」，
# 绝不泄露 owner 凭证 / pairing_code / credential_secret —— 那是 `sim_provision.py`
# 自己的契约（上面 1~5 节仍在守），与"容器跑不跑它"无关。


def test_supervisor_no_longer_wires_the_sim_device_provisioner() -> None:
    """supervisor 既不再 import provisioning，也不再有拉起模拟器的方法/状态。"""
    module = _load_supervisor()
    text = (CLOUDBRIDGE / "supervisor.py").read_text(encoding="utf-8")
    code = ast.parse(text)  # 只看代码构造：注释里解释"这里曾经有什么"是允许的

    imported: set[str] = set()
    for node in ast.walk(code):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "sim_provision" not in imported, (
        "supervisor 不得再 import sim_provision —— 那是模拟器设备 provisioning 的入口")
    assert "sim_phone" not in imported, "supervisor 不得再 import sim_phone"

    funcs = {n.name for n in ast.walk(code)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for gone in ("_start_sim_phone", "_reap_sim_phone", "_sim_lines"):
        assert gone not in funcs, f"supervisor 仍有 {gone}()"

    sup = module.BridgeSupervisor.__new__(module.BridgeSupervisor)
    for gone in ("sim_enabled", "sim_phone", "sim_metrics", "sim_owner_credential",
                 "sim_device_credential", "sim_device_id", "sim_log_dir",
                 "sim_device_name", "sim_join_grace_s", "sim_prompt_wav",
                 "sim_out_wav", "sim_hold_s", "sim_prompt_text"):
        assert not hasattr(sup, gone), f"supervisor 仍持有模拟器状态 {gone}"


def test_provisioning_failure_still_exposes_stage_and_code_only() -> None:
    """安全口径不因移除而失效：失败只暴露「阶段:码」，凭证绝不进异常文本。

    这是上面第 3 节口径的**直读版**（不经过 supervisor）：模块仍由容器外的本地
    harness 使用，凭证泄露在任何调用方都不可接受。
    """
    sp = _load_sim_provision()
    err = sp.SimProvisionError("privacy", 40301)
    assert str(err) == "privacy:40301", f"异常文本必须只是「阶段:码」，实得 {str(err)!r}"
    dumped = json.dumps({"failure": str(err)}, ensure_ascii=False)
    assert OWNER_SECRET not in dumped, "异常文本绝不能泄露 owner 凭证"
    assert CRED_SECRET not in dumped
