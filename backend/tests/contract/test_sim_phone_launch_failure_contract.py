"""契约：手机模拟器**已从产品运行期移除**；它的离线解析仍然可靠（容器外 harness 用）。

历史（为什么曾经有这个文件）
----------------------------
2026-09-12 云端实测：`jax-voice-bridge` 部署后 `simulation.state` 长期停在 `pending`，
而 PG 里 `control_plane_sessions` / `pending_session_claims` / `session_events` **全是 0 行**
——即模拟器从未发起过 `/api/v1/voice/session`。根因是 `_start_sim_phone` 里
`self.sim_phone.start()`（Popen）抛错后**没有兜住**，后台线程静默死掉，状态永远停在
`pending`，与「正在跑」完全无法区分。当时本文件锁死两点：启动异常必须落成
`failed` + `launch:<类型名>`；`status()` 必须把子进程输出暴露出来。

现在（2026-09-17）
------------------
容器内的手机模拟器已**整体移除**（它会在生产容器里起第二个 `--role=phone` Electron，
抢 sidecar 唯一的会话位）；上面那套"启动失败要能被看见"的契约随之失去了对象
——连启动路径都不存在了，也就不存在"静默停在 pending"。移除本身的契约（无启动路径、
环境变量被忽略且可见、`/status` 无 `simulation`）在
`test_bridge_sim_phone_removed_contract.py`。

本文件保留的是**另一半**：`sim_phone.py` 的离线日志解析仍然必须正确 —— 模块没删，
`scripts/sim/run-phone.py` + `scripts/sim/measure-rate-repeat.py` 仍在用它做容器外
唯一的端到端验证。所以这里断言：supervisor 没有那条路径，而模块的解析口径照旧。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cloudbridge"))

import sim_phone  # noqa: E402
import sim_provision  # noqa: E402
import supervisor as sup  # noqa: E402


# --- 移除：启动路径不存在 ----------------------------------------------------


def test_supervisor_no_longer_has_the_sim_phone_launch_path() -> None:
    """那条会静默停在 pending 的后台线程启动路径，已经不存在了。

    这不是"把断言调松"：删除执行路径本身就是修法（测试装置不该住在生产容器里）。
    """
    for gone in ("_start_sim_phone", "_reap_sim_phone", "_sim_lines",
                 "_sim_log_dirs", "_sim_log_files"):
        assert not hasattr(sup.BridgeSupervisor, gone), f"supervisor 仍有 {gone}()"

    s = sup.BridgeSupervisor.__new__(sup.BridgeSupervisor)
    for gone in ("sim_enabled", "sim_phone", "sim_metrics"):
        assert not hasattr(s, gone), f"supervisor 仍持有模拟器状态 {gone}"


def test_sim_modules_survive_for_the_local_harness() -> None:
    """模块不许删：容器外的本地 harness（`scripts/sim/`）仍 `import` 它们。"""
    assert callable(sim_phone.parse_phone_log)
    assert callable(sim_phone.measure_speech_seconds)
    assert hasattr(sim_provision, "resolve_sim_device")


# --- 模块级（仍然有效）：真实落盘形态的解析 ----------------------------------


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


def test_stdout_and_renderer_log_are_both_parsed_by_the_module() -> None:
    """渲染进程日志走文件、stdout 只有噪声 —— 两路并起来才解析得出指标。

    这条并线逻辑原先在 supervisor 的 `_sim_lines()` 里（已随模拟器移除），
    现在只在容器外的 harness（`scripts/sim/run-phone.py`）里；这里守住**模块侧**
    的口径：两路行合起来喂进来，指标必须解析出来（harness 正是这么用的）。
    """
    lines = [
        "[2026-09-16T00:44:20.815Z] [PHONE] 进房成功 123ms",
        "[2026-09-16T00:44:20.818Z] [PHONE] 远端就绪 @900ms",
        "[2026-09-16T00:44:21.182Z] [PHONE] 上行 60帧 / 回复 12帧",
    ]
    metrics = sim_phone.parse_phone_log(lines)
    assert metrics.enter_room_ms == 123
    assert metrics.up_frames == 60 and metrics.reply_frames == 12
    assert metrics.remote_ready_ms == 900, (
        "真实落盘是 sidecar/logger.js:19 的 `[scope] ` 前缀形态 `[PHONE] 远端就绪 @…`；"
        "正则若要求裸 `PHONE `，这里恒为 None"
    )
