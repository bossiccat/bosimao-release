"""容器内音频子系统（PulseAudio）：让 TRTC 的 Linux 原生音频层真的能连上声音服务端。

为什么存在（2026-09-16 线上事故，容器原生日志实测）
--------------------------------------------------
`jax-voice-bridge` 镜像里**没有任何音频子系统**。TRTC 的 Linux 原生层在进房时打：

    pulse_audio_context.cc:119  context state changed to 1  state description a connect
    pulse_audio_context.cc:69   pulse server connect failed, please install pulseaudio
    pulse_audio_context.cc:1327 current context is not ready  GetDevices wait

随后 `EnterRoom` 再无一行原生日志（26 分钟零输出），桥侧权威上行计数
`up rms=%.0f frames=%d`（backend/rtc_bridge/session.py:308）也一条都没出现
⇒ 从没有一帧上行到达桥 ⇒ 手机与云端媒体面**从来没有共处一室**（手机侧 user size 恒为 1）。

本模块负责三件事，**全部 fail-closed**：

1. `build_plan`：组装 pulseaudio 守护进程的 argv 与所需环境变量（纯数据，不启动任何东西）；
2. `wait_for_socket`：判定"真的可用"——**unix socket 出现**才算就绪，只看进程活着不算；
3. `selftest`：构建期自证。本机没有 docker，无法本地构建镜像，所以把
   「这套 argv 在真实 Debian 镜像里到底能不能起来、能不能真的产出 sink/source」
   挪到 `docker build` 里做**实测**（cloudbridge/Dockerfile 的 audio 自证 RUN）：
   起不来就构建失败，绝不产出"哑音频层"的镜像。

三条实现约束（都有实测教训支撑）
--------------------------------
* **必须前台运行**（`--daemonize=no`）：守护化会把日志从 stderr 剥离，而本次事故的
  全部难点就是"看不见"。前台运行时它的输出由 supervisor 的 Child 收到 stdout → CLS。
* **必须显式给设备**（module-null-sink + module-virtual-source）：无头容器里没有声卡，
  只起守护进程的话设备表是空的，TRTC 的 `GetDevices` 依旧拿不到东西。
* **必须显式钉住路径**（XDG_RUNTIME_DIR / PULSE_RUNTIME_PATH / PULSE_SERVER）：
  容器里没有 user session，`XDG_RUNTIME_DIR` 默认不存在，libpulse 客户端与服务端
  各自按它推导 `pulse/native`，不钉住就是两边各自找一个不存在的目录。
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("jax-voice-bridge.audio")

BINARY = "pulseaudio"
PACTL = "pactl"
DEFAULT_RUNTIME_DIR = "/tmp/jax-audio"
SINK_NAME = "jax_null"
# 由 module-null-sink 的 monitor 派生出的"虚拟麦克风"：无头容器里唯一一个
# 非 monitor 的 source，TRTC 的采集设备枚举才能拿到一个像样的输入设备。
SOURCE_NAME = f"{SINK_NAME}.mic"
MONITOR_NAME = f"{SINK_NAME}.monitor"
READY_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 0.25


class AudioSubsystemError(RuntimeError):
    """音频子系统不可用。

    调用方**必须** fail-closed：在 stdout 明确报错后拒绝继续启动 sidecar，
    绝不带着一个哑的音频层进房——那正是本次事故（进房永远完不成且毫无日志）的形态。
    """


@dataclass(frozen=True)
class AudioPlan:
    """音频子系统的完整启动计划（纯数据，不产生任何副作用）。"""

    binary: str
    argv: list[str]
    env: dict[str, str]
    runtime_dir: Path
    socket_path: Path
    sink: str
    source: str

    @property
    def server(self) -> str:
        return self.env["PULSE_SERVER"]


def default_runtime_dir() -> str:
    return os.environ.get("JAX_AUDIO_RUNTIME_DIR", "").strip() or DEFAULT_RUNTIME_DIR


def build_plan(runtime_dir=None, *, sink: str = SINK_NAME,
               which=shutil.which) -> AudioPlan:
    """组装启动计划；任何前提不满足即抛 AudioSubsystemError（fail-closed）。"""
    binary = which(BINARY) or ""
    if not binary:
        raise AudioSubsystemError(
            "找不到 pulseaudio 可执行文件（镜像 apt 段必须安装 pulseaudio，"
            "见 cloudbridge/Dockerfile）；没有它，TRTC 原生层的 "
            "`pulse server connect failed` 会让 EnterRoom 永远完不成"
        )

    runtime = Path(runtime_dir or default_runtime_dir())
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        # 0o700：套接字目录只有本容器内的进程可达，配合 auth-anonymous 不产生额外暴露面。
        os.chmod(runtime, 0o700)
    except OSError as exc:
        raise AudioSubsystemError(f"音频运行时目录不可用（{runtime}）：{exc}") from exc

    pulse_dir = runtime / "pulse"
    socket_path = pulse_dir / "native"
    env = {
        # 容器里没有 user session ⇒ XDG_RUNTIME_DIR 默认不存在。libpulse 客户端与服务端
        # 都按它推导套接字目录，不钉住就是两边各自找一个不存在的目录（本次事故形态）。
        "XDG_RUNTIME_DIR": str(runtime),
        # 再显式钉一遍运行时路径，抵消发行版/自编译 libpulse 对默认路径的推导差异。
        "PULSE_RUNTIME_PATH": str(pulse_dir),
        # TRTC 原生层走 libpulse 客户端；PULSE_SERVER 是客户端侧优先级最高的显式指定，
        # 最不容易被"默认路径推导"吃掉。
        "PULSE_SERVER": f"unix:{socket_path}",
    }

    argv = [
        binary,
        # 前台运行：日志走 stderr → supervisor → stdout → CLS（可检索）。
        # 守护化（--daemonize）会把日志交给 syslog，容器里没有 syslog ⇒ 又变成看不见。
        "--daemonize=no",
        # 空闲不自动退出（默认 20s）：没有客户端时退出会让 TRTC 后续连接直接失败。
        "--exit-idle-time=-1",
        # 客户端不能请求它退出。
        "--disallow-exit=yes",
        # 不写 pid 文件：容器原地重启时残留 pid 文件会造成"已在运行"的假象。
        "--use-pid-file=no",
        # CloudRun 的 /dev/shm 很小（Chromium 因此已加 --disable-dev-shm-usage）；
        # 关掉 POSIX 共享内存后客户端一律走套接字，避免 shm 分配失败。
        "--disable-shm=yes",
        "--log-target=stderr",
        # 不加载发行版 default.pa：只装下面显式声明的模块，行为完全可控。
        "-n",
        # 客户端入口（unix 套接字）。auth-anonymous=1：容器内单租户、套接字目录 0700，
        # 免掉 cookie/HOME 不一致导致"客户端连不上"的整类问题。
        "--load=module-native-protocol-unix auth-anonymous=1",
        # 设备：无头容器没有声卡，必须自己造。顺序有意义——virtual-source 的 master
        # 是 null-sink 的 monitor，所以 null-sink 必须先加载。
        f"--load=module-null-sink sink_name={sink}"
        " sink_properties=device.description=JaxNullSink",
        f"--load=module-virtual-source source_name={SOURCE_NAME}"
        f" master={MONITOR_NAME}"
        " source_properties=device.description=JaxNullMic",
    ]
    return AudioPlan(
        binary=binary,
        argv=argv,
        env=env,
        runtime_dir=runtime,
        socket_path=socket_path,
        sink=sink,
        source=SOURCE_NAME,
    )


def wait_for_socket(socket_path: Path, *, timeout: float = READY_TIMEOUT_S,
                    poll: float = POLL_INTERVAL_S, sleep=time.sleep,
                    alive=None) -> bool:
    """等到 unix 套接字**真的出现**才算就绪；进程已死则立刻放弃（不空等超时）。

    只看"进程还活着"是不够的：pulseaudio 会在套接字就绪前先起进程，
    而 TRTC 只要连不上就 `GetDevices wait`——本函数判的就是那一步。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            # 不做 Path() 强转：本机（Windows）无法创建 AF_UNIX 套接字，
            # 契约测试用一个只带 is_socket() 的替身即可把"就绪"这条路也走通。
            if socket_path.is_socket():
                return True
        except (OSError, AttributeError):
            pass
        if alive is not None and not alive():
            return False
        if time.monotonic() >= deadline:
            return False
        sleep(poll)


def _pactl_lines(kind: str, *, env: dict[str, str], which=shutil.which,
                 run=subprocess.run) -> list[str] | None:
    """`pactl list short <kind>` 的行；pactl 不可用或调用失败返回 None（不得当成"设备为空"）。"""
    pactl = which(PACTL) or ""
    if not pactl:
        return None
    try:
        done = run([pactl, "list", "short", kind],
                   capture_output=True, text=True, env=env, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "").splitlines()


def inspect_devices(*, env: dict[str, str], sink: str,
                    which=shutil.which, run=subprocess.run) -> dict:
    """清点音频设备：TRTC 的设备枚举必须有东西可拿，否则依旧 GetDevices wait。"""
    sinks = _pactl_lines("sinks", env=env, which=which, run=run)
    sources = _pactl_lines("sources", env=env, which=which, run=run)
    return {
        "pactl": (which(PACTL) or "") != "",
        "sinks": sinks,
        "sources": sources,
        "sink_present": bool(sinks) and any(sink in line for line in sinks),
        # monitor 与 virtual-source 名字都以 sink 名为前缀，故按前缀判定即可。
        "source_present": bool(sources) and any(sink in line for line in sources),
    }


def _report(message: str) -> None:
    print(message, flush=True)


def _echo(log_path: Path, log) -> None:
    """把守护进程自己的日志原样转到 stdout——死因必须一眼可见。"""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        log(f"[audio] pulseaudio| {line}")


def selftest(*, runtime_dir=None, timeout: float = READY_TIMEOUT_S,
             require_devices: bool = True, popen=subprocess.Popen,
             which=shutil.which, run=subprocess.run, sleep=time.sleep,
             log=_report) -> int:
    """在**当前真实环境**里用与运行期同一套 argv 起一次，并清点设备。

    返回进程退出码语义：0 = 通过，1 = fail-closed。
    本机（Windows，无 pulseaudio、无 docker）跑不出 0；它是给 `docker build` 用的，
    参数全部可注入是为了让契约测试能用替身把每条失败分支都逼出来。
    """
    try:
        plan = build_plan(runtime_dir, which=which)
    except AudioSubsystemError as exc:
        log(f"[audio] FATAL: {exc}")
        return 1

    log(f"[audio] self-test: cwd-agnostic argv={' '.join(plan.argv)}")
    env = {**os.environ, **plan.env}
    log_path = plan.runtime_dir / "pulseaudio-selftest.log"
    with open(log_path, "wb") as sink:
        proc = popen(plan.argv, stdout=sink, stderr=subprocess.STDOUT, env=env)
    try:
        ready = wait_for_socket(plan.socket_path, timeout=timeout, sleep=sleep,
                               alive=lambda: proc.poll() is None)
        if not ready:
            log(f"[audio] FATAL: pulseaudio 在 {timeout}s 内未就绪"
                f"（socket={plan.socket_path} 未出现，进程 exit={proc.poll()}）")
            _echo(log_path, log)
            return 1
        log(f"[audio] self-test: 套接字就绪 -> {plan.socket_path}")
        devices = inspect_devices(env=env, sink=plan.sink, which=which, run=run)
        _echo(log_path, log)

        if not devices["pactl"]:
            log("[audio] FATAL: pactl 不可用（pulseaudio-utils 未安装），无法清点音频设备；"
                "设备是否真的存在是本自证的核心，不能跳过")
            return 1
        if require_devices:
            if not devices["sink_present"]:
                log(f"[audio] FATAL: 音频子系统里没有 sink={plan.sink} —— "
                    f"TRTC 的播放设备枚举会拿到空表（GetDevices wait）")
                log(f"[audio] sinks={devices['sinks']}")
                return 1
            if not devices["source_present"]:
                log(f"[audio] FATAL: 音频子系统里没有 source≈{plan.sink}（monitor 或虚拟麦）"
                    " —— TRTC 的采集设备枚举会拿到空表（GetDevices wait）")
                log(f"[audio] sources={devices['sources']}")
                return 1
        log(f"[audio] self-test OK: sink={plan.sink} source={plan.source} "
            f"sinks={len(devices['sinks'] or [])} sources={len(devices['sources'] or [])}")
        return 0
    finally:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                proc.kill()


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="语音桥容器音频子系统（PulseAudio）：构建期自证 / 运行期参数组装")
    parser.add_argument("--self-test", action="store_true",
                       help="在当前环境起一次 pulseaudio 并清点 sink/source（构建期用）")
    parser.add_argument("--runtime-dir", default=None,
                       help=f"XDG_RUNTIME_DIR（默认 {DEFAULT_RUNTIME_DIR}）")
    parser.add_argument("--timeout", type=float, default=READY_TIMEOUT_S)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        return selftest(runtime_dir=args.runtime_dir, timeout=args.timeout)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
