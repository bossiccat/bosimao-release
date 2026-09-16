"""契约：模拟器「启动失败」必须被记录，绝不能静默停在 pending。

为什么需要
----------
2026-09-12 云端实测：`jax-voice-bridge` 部署后 `simulation.state` 长期停在 `pending`，
而 PG 里 `control_plane_sessions` / `pending_session_claims` / `session_events` **全是 0 行**
——即模拟器从未发起过 `/api/v1/voice/session`。根因是 `_start_sim_phone` 里
`self.sim_phone.start()`（Popen）抛错后**没有兜住**，后台线程静默死掉，状态永远停在
`pending`，与「正在跑」完全无法区分。这正是本项目反复吃过的静默失败模式。

本测试锁死两点：启动异常必须落成 `failed` + `launch:<类型名>`；`status()` 必须把子进程
输出暴露出来，让死因可读。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cloudbridge"))

import sim_phone  # noqa: E402
import sim_provision  # noqa: E402
import supervisor as sup  # noqa: E402


class _BoomChild:
    """start() 直接抛错的替身：模拟容器内缺 xvfb-run / electron 的情况。"""

    def __init__(self, *args, **kwargs) -> None:
        self.tail: list[str] = []
        self.exit_code = None

    def start(self) -> None:
        raise OSError("xvfb-run missing")

    def describe(self) -> dict:
        return {"alive": False, "pid": None, "starts": 0, "exit_code": self.exit_code,
                "output_tail": list(self.tail)}


def test_launch_failure_is_recorded_not_silent(monkeypatch, tmp_path):
    s = sup.BridgeSupervisor()
    s.sim_enabled = True
    monkeypatch.setattr(
        sim_provision, "resolve_sim_device",
        lambda **kwargs: sim_provision.SimDevice(
            device_id="dev-1", credential_token="dev-1.secret", expires_at=""
        ),
    )
    monkeypatch.setattr(sim_phone, "ensure_prompt_wav", lambda *a, **k: tmp_path / "prompt.wav")
    monkeypatch.setattr(sup, "Child", _BoomChild)

    s._start_sim_phone()

    assert s.sim_metrics.state == "failed", "启动异常必须落成 failed，而不是留在 pending"
    assert s.sim_metrics.failure == "launch:OSError"
    # 配对成功的证据要能被读到（非敏感）
    assert getattr(s, "_sim_device_id", "") == "dev-1"


def test_status_exposes_simulation_child_output(monkeypatch, tmp_path):
    """status() 必须暴露 sim 子进程的 exit_code/output_tail，否则静默失败无从归因。"""
    s = sup.BridgeSupervisor()
    s.sim_enabled = True
    s.sim_phone = _BoomChild()
    s.sim_phone.tail = ["PHONE 签发失败 code=40101"]

    payload = s.status()

    sim = payload["simulation"]
    assert "child" in sim, "simulation 必须含 child 段"
    assert sim["child"]["output_tail"] == ["PHONE 签发失败 code=40101"]
    assert "device_id" in sim


def test_sim_argv_carries_control_plane_url(monkeypatch, tmp_path):
    """必须显式传 --sign-url：config.js 默认值是本地 https://127.0.0.1:8000。

    2026-09-12 实测漏传该参数 → 模拟器去连容器本机 8000，/session 从未发出
    （PG 里 control_plane_sessions 为 0 行）。
    """
    captured: dict = {}

    class _Capture:
        def __init__(self, name, argv, cwd, extra_env, **kwargs):
            captured["argv"] = argv
            captured["env"] = extra_env
            self.tail: list[str] = []
            self.exit_code = None

        def start(self) -> None:
            captured["started"] = True

        def describe(self) -> dict:
            return {"alive": False, "pid": None, "starts": 0, "exit_code": None, "output_tail": []}

    s = sup.BridgeSupervisor()
    s.sim_enabled = True
    s.sign_url = "https://control-plane.example"
    s.sim_log_dir = tmp_path / "logs"
    monkeypatch.setattr(
        sim_provision, "resolve_sim_device",
        lambda **kwargs: sim_provision.SimDevice(
            device_id="dev-9", credential_token="dev-9.secret", expires_at=""
        ),
    )
    monkeypatch.setattr(sim_phone, "ensure_prompt_wav", lambda *a, **k: tmp_path / "p.wav")
    monkeypatch.setattr(sup, "Child", _Capture)

    s._start_sim_phone()

    argv = captured["argv"]
    assert f"--sign-url={s.sign_url}" in argv, "argv 必须带控制面地址"
    assert "--device=dev-9" in argv, "device 必须是注册返回的 UUID"
    assert captured["env"]["JAX_SIDECAR_LOG_DIR"] == str(s.sim_log_dir)
    # 凭证不得出现在 argv（会进 Child.start 的日志）
    assert not any("dev-9.secret" in str(a) for a in argv)


def test_sim_lines_unions_stdout_and_renderer_log(tmp_path):
    """渲染进程日志走文件，指标解析必须把文件并进来，否则永远解析不到。"""
    s = sup.BridgeSupervisor()
    s.sim_log_dir = tmp_path
    (tmp_path / "sidecar-phone.log").write_text(
        "[t] [PHONE] 进房成功 123ms\n[t] [PHONE] 上行 60帧 / 回复 12帧\n", encoding="utf-8"
    )
    child = _BoomChild()
    child.tail = ["[PHONE] 远端就绪 @900ms"]

    lines = s._sim_lines(child)

    assert any("远端就绪" in l for l in lines)
    assert any("进房成功" in l for l in lines)
    metrics = sim_phone.parse_phone_log(lines)
    assert metrics.enter_room_ms == 123
    assert metrics.up_frames == 60 and metrics.reply_frames == 12
    # 缺口只有这一行：夹具从一开始就是**真实落盘形态**（带 `[PHONE] ` 前缀），
    # 但此前只断言了 enter_room/up/reply 三项，把 `remote_ready_ms` 漏在断言之外，
    # 于是正则要求裸 `PHONE ` 这个缺陷一直没有用例会红（实测 e2e-summary.json 里恒为 null）。
    assert metrics.remote_ready_ms == 900, (
        "真实落盘是 sidecar/logger.js:19 的 `[scope] ` 前缀形态 `[PHONE] 远端就绪 @…`；"
        "正则若要求裸 `PHONE `，这里恒为 None"
    )


def test_sign_fail_code_is_parsed_from_the_real_prefixed_format() -> None:
    """`签发失败 code=` 必须能从**真实落盘形态**解析出业务码。

    `sidecar/logger.js:19` 统一落成 `[ISO] [PHONE] msg`，所以真机上这行是
    `[PHONE] 签发失败 code=40101`。旧正则要求裸 `PHONE ` ⇒ 永远解析不出 code，
    状态端点就分不清「被 privacy 门禁拒」与「被限流/凭证拒」——这正是
    `PhoneSimMetrics.failure` 保留业务码的全部意义（见 cloudbridge/sim_phone.py:144-148）。
    """
    prefixed = sim_phone.parse_phone_log(
        ["[2026-09-16T00:44:20.298Z] [PHONE] 签发失败 code=40101"]
    )
    assert prefixed.state == "failed"
    assert prefixed.failure == "PHONE_SESSION_SIGN_FAILED:40101", (
        f"真实 `[PHONE] ` 形态必须解析出业务码，实得 {prefixed.failure!r}"
    )

    # 历史日志与部分合成夹具是裸 `PHONE ` 形态，必须继续兼容。
    bare = sim_phone.parse_phone_log(["PHONE 签发失败 code=-1002"])
    assert bare.failure == "PHONE_SESSION_SIGN_FAILED:-1002", (
        f"裸 `PHONE ` 形态必须继续兼容，实得 {bare.failure!r}"
    )


def test_remote_not_ready_note_is_parsed_from_the_real_prefixed_format() -> None:
    """同一条「要求裸 `PHONE `」的正则缺陷还有第三处：`远端未就绪（Nms 超时）`。

    漏掉它不会让数字变错，会让**归因信息凭空消失**：超时后继续上行的这一轮
    本该留一条 `remote_not_ready` note，实测因为前缀不匹配一条也没留下
    （`sidecar/logger.js:19` 落盘的是 `[PHONE] 远端未就绪（…）`）。
    """
    m = sim_phone.parse_phone_log(
        ["[2026-09-16T00:44:20.298Z] [PHONE] 远端未就绪（25000ms 超时，继续上行）"]
    )
    assert "remote_not_ready" in m.notes, f"必须记下超时 note，实得 {m.notes}"

