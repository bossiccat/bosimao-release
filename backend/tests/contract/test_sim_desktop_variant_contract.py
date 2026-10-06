"""契约：check3 desktop 变体（SIM_CHECK3_MODE=desktop 生产拓扑两腿验收）。

背景
----
生产桌面拓扑里，本地 `--role=sidecar` 与云端常驻 sidecar 用**同一份**
VOICE_SIDECAR_CREDENTIAL 抢 intent，云端容器在内网占尽先机 → 本地永远抢不到，
旧 check3（本地 sidecar 媒体对账）是 dead-by-design。裁决后的两腿变体：

  desktop 腿：编排器用 owner 凭证 provision 一台 `platform=windows` 的模拟桌面
    设备，run-sidecar.py 以 `--role=desktop` 拉起（凭证经
    SIM_DESKTOP_DEVICE_CREDENTIAL → VOICE_DESKTOP_DEVICE_CREDENTIAL 注入），
    验证「自发起会话 + 云端 jax-pc-sidecar 进同一间房」的生产汇合语义；
  phone 腿：原样（run-phone.py），intent 由云端 sidecar 确定性领取，媒体对账依旧。

硬约束
------
- SIM_SIDECAR_ROLE / SIM_CHECK3_MODE 缺省时行为与历史逐字节一致
  （check1/check2 证据可复现）；
- 不改 sidecar 运行时 JS（test/desktop-local-loop.test.js:55 静态断言禁止
  desktop 碰 pending/bridge），不改 cloudbridge 生产运行时。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]

PAIRING_SECRET = "pc-secret-desktop-abcdefghijklmnop"
CRED_SECRET = "cred-secret-desktop-do-not-leak"
DEVICE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _load(module_name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_sim_provision():
    return _load("jax_sim_provision_desktop", "cloudbridge/sim_provision.py")


def _load_run_sidecar():
    return _load("jax_sim_run_sidecar_desktop", "scripts/sim/run-sidecar.py")


def _load_run_e2e():
    return _load("jax_sim_run_e2e_desktop", "scripts/sim/run-sim-e2e.py")


def _stub_request(sp):
    responses = {
        sp.PRIVACY_CLOUD_PROCESSING_PATH: (200, {"code": 0, "data": {}}),
        sp.PAIRING_CODE_PATH: (
            200, {"code": 0, "data": {"pairing_code": PAIRING_SECRET}},
        ),
        sp.REGISTER_PATH: (
            201, {"code": 0, "data": {"device_id": DEVICE_ID,
                                      "credential_secret": CRED_SECRET}},
        ),
    }
    calls: list[dict] = []

    def fake_request(method, url, payload, *, owner_credential="", device_token="",
                     timeout_s=20):
        calls.append({"method": method, "url": url, "payload": payload})
        for path, resp in responses.items():
            if url.endswith(path):
                return resp
        raise AssertionError(f"unexpected url: {url}")

    return fake_request, calls


DOT = {"RTC_BRIDGE_CONTROL_PLANE_BASE_URL": "https://cp.example/"}


# --- 1. provision_sim_device 的 platform 参数 -------------------------------


def test_provision_platform_defaults_to_android() -> None:
    sp = _load_sim_provision()
    fake_request, calls = _stub_request(sp)
    with mock.patch.object(sp, "_request", fake_request):
        sp.provision_sim_device("https://cp.example", "owner", device_name="d")
    assert calls[1]["payload"] == {"platform": "android", "device_name_hint": "d"}
    assert calls[2]["payload"]["platform"] == "android"


def test_provision_platform_windows_reaches_both_payloads() -> None:
    sp = _load_sim_provision()
    fake_request, calls = _stub_request(sp)
    with mock.patch.object(sp, "_request", fake_request):
        sp.provision_sim_device(
            "https://cp.example", "owner",
            device_name="jax-sim-desktop", platform="windows",
        )
    assert calls[1]["payload"] == {
        "platform": "windows", "device_name_hint": "jax-sim-desktop",
    }
    assert calls[2]["payload"] == {
        "pairing_code": PAIRING_SECRET,
        "device_name": "jax-sim-desktop",
        "platform": "windows",
    }


# --- 2. run-sidecar.py 计划构造 ---------------------------------------------


def test_resolve_sim_role_defaults_to_sidecar_and_rejects_unknown() -> None:
    rs = _load_run_sidecar()
    assert rs.resolve_sim_role({}) == "sidecar"
    assert rs.resolve_sim_role({"SIM_SIDECAR_ROLE": "desktop"}) == "desktop"
    with pytest.raises(SystemExit):
        rs.resolve_sim_role({"SIM_SIDECAR_ROLE": "phone"})


def test_run_sidecar_default_plan_is_byte_identical(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    rs = _load_run_sidecar()
    base_env = {"A": "1"}
    argv, env = rs.build_launch(
        DOT, base_env, role="sidecar", electron="ELECTRON",
    )
    assert argv == [
        "ELECTRON", ".", "--role=sidecar",
        "--bridge-url=ws://127.0.0.1:19092",
        "--sign-url=https://cp.example",
    ]
    assert env == base_env, "sidecar 模式不得注入/改动任何环境变量"
    assert "VOICE_DESKTOP_DEVICE_CREDENTIAL" not in env


def test_run_sidecar_desktop_plan_omits_bridge_url_and_injects_credential(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    rs = _load_run_sidecar()
    argv, env = rs.build_launch(
        DOT, {"A": "1"}, role="desktop", electron="ELECTRON",
        desktop_device_id="dev-1", desktop_credential="dev-1.secret",
    )
    assert argv == [
        "ELECTRON", ".", "--role=desktop",
        "--device=dev-1",
        "--sign-url=https://cp.example",
    ]
    assert not any(a.startswith("--bridge-url=") for a in argv), (
        "desktop 拓扑没有本地桥，argv 不得携带 --bridge-url"
    )
    assert env["VOICE_DESKTOP_DEVICE_CREDENTIAL"] == "dev-1.secret"
    assert env["A"] == "1", "其余环境变量必须原样透传"


@pytest.mark.parametrize(
    "device_id,credential,missing_key",
    [
        ("", "x.secret", "SIM_DESKTOP_DEVICE_ID"),
        ("dev-1", "", "SIM_DESKTOP_DEVICE_CREDENTIAL"),
    ],
)
def test_run_sidecar_desktop_plan_fails_fast_on_missing_device_env(
    monkeypatch, tmp_path, device_id, credential, missing_key,
) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    rs = _load_run_sidecar()
    with pytest.raises(SystemExit) as excinfo:
        rs.build_launch(
            DOT, {}, role="desktop", electron="ELECTRON",
            desktop_device_id=device_id, desktop_credential=credential,
        )
    assert missing_key in str(excinfo.value), "报错必须指名缺哪个 env"
    if credential:
        assert credential not in str(excinfo.value), "报错文本不得携带凭证"


# --- 3. run-sim-e2e.py 模式决策（只测决策，不碰子进程） ----------------------


def test_e2e_default_mode_keeps_bridge_steps_and_legacy_marker(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    e2 = _load_run_e2e()
    plan = e2.build_plan(e2.resolve_check3_mode({}))
    assert plan["spawn_bridge"] is True
    assert plan["read_bridge_metrics"] is True
    assert plan["desktop_leg"] is False
    assert e2.SIDECAR_READY_MARKER == "[SIG] 意图轮询已启动"


def test_e2e_desktop_mode_skips_bridge_and_sets_desktop_markers(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    e2 = _load_run_e2e()
    plan = e2.build_plan(e2.resolve_check3_mode({"SIM_CHECK3_MODE": "desktop"}))
    assert plan["spawn_bridge"] is False, "desktop 拓扑没有本地桥"
    assert plan["read_bridge_metrics"] is False
    assert plan["desktop_leg"] is True
    assert plan["ready_timeout_s"] == 90
    assert e2.DESKTOP_JOIN_MARKER in "[DESKTOP] 进房成功 elapsed=200"
    assert (
        e2.DESKTOP_PEER_MARKER in
        "[DESKTOP] 模型端已进房 userId=jax-pc-sidecar"
    )


def test_e2e_unknown_check3_mode_fails_fast(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    e2 = _load_run_e2e()
    with pytest.raises(SystemExit):
        e2.resolve_check3_mode({"SIM_CHECK3_MODE": "bridge"})


# --- 4. desktop 汇合标记扫描（纯函数，喂真实落盘形态） ----------------------


def test_desktop_rendezvous_progress_detects_join_then_cloud_peer(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    e2 = _load_run_e2e()
    lines = [
        "[2026-10-06T00:00:01.000Z] [BOOT] role=desktop",
        "[2026-10-06T00:00:02.000Z] [DESKTOP] 进房成功 elapsed=200",
        "[2026-10-06T00:00:02.500Z] [DESKTOP] 本机麦克风已开启，回复由房间播放到喇叭",
        "[2026-10-06T00:00:03.000Z] [DESKTOP] 模型端已进房 userId=jax-pc-sidecar",
    ]
    join, peer = e2.desktop_rendezvous_progress(lines, start_epoch=0)
    assert join is True and peer is True

    only_join, no_peer = e2.desktop_rendezvous_progress(lines[:2], start_epoch=0)
    assert only_join is True and no_peer is False


# --- 5. 相对 SIM_OUT_DIR 的日志 split-brain 防线 ----------------------------
#
# 2026-10-06 实跑实锤（outputs/check3-desktop-20261006/）：SIM_OUT_DIR 为相对路径时，
# run-sidecar.py 把相对 LOGDIR 传给 electron，而 electron 子进程 cwd=sidecar/ ⇒
# logger.js/main.js 的 path.resolve 把日志写到 sidecar/outputs/... 下；编排器却在
# 仓库根的 outputs/... 扫描 ⇒ 桌面腿汇合明明成立（sidecar-desktop.log 里
# 进房成功 + 模型端已进房俱全）却被判超时——测量假阴性。防线：三个脚本读
# SIM_OUT_DIR 处一律 .resolve()，保证传给子进程的日志目录是绝对路径。


def test_e2e_out_dir_is_resolved_absolute(monkeypatch) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", "outputs/some-relative-dir")
    e2 = _load_run_e2e()
    assert e2.D.is_absolute(), "编排器证据目录必须绝对化，否则子进程 cwd 不同导致日志分裂"


def test_run_sidecar_out_dir_is_resolved_absolute(monkeypatch) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", "outputs/some-relative-dir")
    rs = _load_run_sidecar()
    assert rs.OUT_DIR.is_absolute(), "sidecar 日志目录必须绝对化（electron cwd=sidecar/）"


def test_run_phone_out_dir_is_resolved_absolute(monkeypatch) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", "outputs/some-relative-dir")
    rp = _load("jax_sim_run_phone_desktop", "scripts/sim/run-phone.py")
    assert rp.D.is_absolute(), "phone 日志目录必须绝对化（electron cwd=sidecar/）"


def test_desktop_rendezvous_progress_ignores_stale_and_foreign_peer(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("SIM_OUT_DIR", str(tmp_path))
    e2 = _load_run_e2e()
    lines = [
        "[2026-10-06T00:00:02.000Z] [DESKTOP] 进房成功 elapsed=200",
        "[2026-10-06T00:00:03.000Z] [DESKTOP] 模型端已进房 userId=jax-pc-sidecar",
    ]
    # 上一轮的旧行（时间戳早于 start_epoch）不得算作本轮证据
    join, peer = e2.desktop_rendezvous_progress(lines, start_epoch=10**12)
    assert join is False and peer is False

    # 别的远端用户（例如手机模拟器）不算云端 sidecar 汇合
    foreign = ["[2026-10-06T00:00:03.000Z] [DESKTOP] 模型端已进房 userId=some-phone"]
    join, peer = e2.desktop_rendezvous_progress(foreign, start_epoch=0)
    assert join is False and peer is False
