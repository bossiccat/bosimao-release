"""模拟启动器 · 第 3 步：手机模拟器（--role=phone）。

它做真手机做的事：用真实配对换来的设备凭证调云端 /session → 进同一个 TRTC 房间 →
按 20ms 节拍推一段中文语音 → 等 sidecar 回复 → 写回 wav 并报首包延迟。

三个必须处理的点（都是本机环境特有，云端容器里也一样踩过）：
  · ELECTRON_RUN_AS_NODE=1 必须清掉，否则 electron 退化成纯 Node；
  · NODE_EXTRA_CA_CERTS 必须给（main.js fail-closed 守卫）；
  · --sign-url 必须显式指向云端（config.js 默认是 127.0.0.1:8000）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "sidecar"
sys.path.insert(0, str(ROOT / "cloudbridge"))

import sim_phone  # noqa: E402
import sim_provision  # noqa: E402

D = ROOT / "outputs" / "deploy-backup-20260911"
LOGDIR = D / "sidecar-logs-phone"
OUT = D / "local-phone.out.log"


_TRUTHY = {"1", "true", "yes", "on"}


def _barge_in_disabled() -> bool:
    """SIM_BARGE_IN_DISABLE 为真时，本轮完全不测打断（不生成、不注入任何打断变量）。

    为什么必须有这个开关（2026-09-13 测量脚手架污染实锤）：
    sidecar/phone.js:181 在收到**首包回复帧**时就调 scheduleBargeIn()，于是每轮
    模拟的回复都会在首帧后 800ms 被插话打断（审计统计 43 次回复里 29 次被真实
    打断，近期几乎 100%）。⇒ 所有「回复不完整/偏快」的读数都可能是**我们自己的
    测量脚手架**造成的，而非产品缺陷。做「回复速率/完整性」这类测量时必须关掉。
    """
    return os.environ.get("SIM_BARGE_IN_DISABLE", "").strip().lower() in _TRUTHY


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def main() -> int:
    dot = load_env()
    api = dot["RTC_BRIDGE_CONTROL_PLANE_BASE_URL"].rstrip("/")

    # 真实配对换设备凭证
    device = sim_provision.resolve_sim_device(
        base_url=api, explicit_token="", owner_credential=dot["VOICE_OWNER_CREDENTIAL"],
        device_name="jax-sim-phone",
    )
    print("device_id =", device.device_id)

    # 中文提示音（edge-tts + ffmpeg）；已存在则复用。
    # 提示词可用 SIM_PROMPT_TEXT 覆盖：默认那句是「用一句话介绍你自己」，
    # 模型只答 ~2s 属正确长度；要验证「长回答是否会被截断」必须换成要求长回答的提示词。
    prompt_text = os.environ.get("SIM_PROMPT_TEXT") or sim_phone.DEFAULT_PROMPT_TEXT
    wav_name = os.environ.get("SIM_PROMPT_WAV_NAME") or "sim-prompt.wav"
    try:
        wav = sim_phone.ensure_prompt_wav(D / wav_name, text=prompt_text)
        print("prompt text =", prompt_text)
        print("prompt wav  =", wav, wav.stat().st_size, "bytes")
    except Exception as exc:  # noqa: BLE001
        print("FATAL: 提示音生成失败:", type(exc).__name__, str(exc)[:200])
        return 1

    env = dict(os.environ)
    env.update(dot)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["NODE_EXTRA_CA_CERTS"] = dot.get("RTC_BRIDGE_CONTROL_PLANE_CA_FILE", "")
    env["JAX_SIDECAR_LOG_DIR"] = str(LOGDIR)
    env["VOICE_SIM_DEVICE_CREDENTIAL"] = device.credential_token
    # 打断（barge-in）测量：首包回复后 800ms 推一段**人声**插话，量停止延迟。
    # 打断用的语音必须是真实人声（模型按 VAD 判定插话），故同样用 edge-tts 合成。
    # SIM_BARGE_IN_DISABLE=1 时整块跳过（见 _barge_in_disabled 说明）——默认行为不变。
    if _barge_in_disabled():
        print("本轮不测打断（SIM_BARGE_IN_DISABLE=1）")
    else:
        barge_text = os.environ.get("SIM_BARGE_IN_TEXT") or "等一下，你先停一下，别说了。"
        try:
            barge_wav = sim_phone.ensure_prompt_wav(D / "sim-barge-in.wav", text=barge_text)
            env["SIM_BARGE_IN_WAV"] = str(barge_wav)
            env["SIM_BARGE_IN_AFTER_MS"] = os.environ.get("SIM_BARGE_IN_AFTER_MS", "800")
            print("barge-in text =", barge_text)
        except Exception as exc:  # noqa: BLE001
            print("警告: 打断提示音生成失败，本轮不测打断:", type(exc).__name__)
    LOGDIR.mkdir(parents=True, exist_ok=True)

    argv = [
        str(SIDECAR / "node_modules" / "electron" / "dist" / "electron.exe"),
        ".", "--role=phone", f"--device={device.device_id}",
        f"--sign-url={api}", f"--wav={wav}",
        f"--out-wav={D / 'sim-reply.wav'}",
        "--hold=45", "--join-grace=25",
    ]
    print("sign_url =", api, "| join-grace = 25s | hold = 45s")
    with OUT.open("wb") as fh:
        proc = subprocess.Popen(argv, cwd=str(SIDECAR), env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    rc = proc.wait()
    print("phone exit =", rc)

    # 回读渲染进程日志并解析指标
    lines: list[str] = []
    for name in ("sidecar-phone.log", "sidecar-main-diag.log"):
        f = LOGDIR / name
        if f.is_file():
            lines.append(f"===== {name} =====")
            lines.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
    m = sim_phone.parse_phone_log(lines)
    import json
    print("METRICS:", json.dumps(m.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
