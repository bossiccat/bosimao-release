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
  设备属性里的 `device.form_factor` 同样**不是可选项**：对 libliteavsdk.so 做全库串扫描 +
  取址交叉引用后，SDK 真正读取的 `device.*` 属性只有 `device.description` 与
  `device.form_factor` 两个（`device.class` 零命中）。缺 form_factor 的那一版正对应
  线上"PA 里有 sink、TRTC 的播放设备表却为空"（code 1202）的现场。
* **必须显式钉住路径**（XDG_RUNTIME_DIR / PULSE_RUNTIME_PATH / PULSE_SERVER）：
  容器里没有 user session，`XDG_RUNTIME_DIR` 默认不存在，libpulse 客户端与服务端
  各自按它推导 `pulse/native`，不钉住就是两边各自找一个不存在的目录。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
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
        #
        # `device.form_factor` 不是"补全得像样点"，它是**证据决定的一处**：
        # 对 libliteavsdk.so（12.5.705-beta.0，sha256 c5a6f1df…）做全库可打印串扫描 +
        # RIP 相对取址交叉引用，`device.*` 前缀的属性键**只有两个**被 SDK 读：
        #   · device.description —— 我们本来就给了；
        #   · device.form_factor —— 我们此前**完全没给**。
        # 而 `device.class` 在 14.9MB 的 .so 里**一次都没出现**（0 命中），
        # 所以不按"看起来更像真设备"去补 `device.class=sound`（无证据支持，宁可不加）。
        # 属性值取 PA 的规范取值：播放端 speaker、采集端 microphone。
        f"--load=module-null-sink sink_name={sink}"
        " sink_properties=device.description=JaxNullSink,device.form_factor=speaker",
        f"--load=module-virtual-source source_name={SOURCE_NAME}"
        f" master={MONITOR_NAME}"
        " source_properties=device.description=JaxNullMic,device.form_factor=microphone",
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


def _pactl_info(*, env: dict[str, str], which=shutil.which,
                run=subprocess.run) -> str | None:
    """`pactl info` 的文本；pactl 不可用或调用失败返回 None（与"没有默认设备"是两件事）。"""
    pactl = which(PACTL) or ""
    if not pactl:
        return None
    try:
        done = run([pactl, "info"], capture_output=True, text=True, env=env, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout or ""


def _pactl_list(kind: str, *, env: dict[str, str], which=shutil.which,
                run=subprocess.run) -> str | None:
    """`pactl list <kind>`（**长**格式）的文本；不可用或调用失败返回 None。"""
    pactl = which(PACTL) or ""
    if not pactl:
        return None
    try:
        done = run([pactl, "list", kind], capture_output=True, text=True,
                   env=env, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout or ""


_DEVICE_BLOCK_RE = re.compile(r"^(Sink|Source) #\d+")
# 端口行：恰好两个制表符 + 端口名 + 冒号。真实输出里端口下面还会嵌一层
# `\t\t\tproperties:` / `\t\t\t\tdevice.icon_name = …`，所以层级必须**钉死两格**，
# 否则端口下的属性会污染属性键清单（那正是本项观测要看的两个值之一）。
_PORT_LINE_RE = re.compile(r"^\t\t(?P<port>[^\t\s:]+):")
# 属性行：恰好两个制表符 + key = value（`pactl list sinks` 形如
# `\t\tdevice.class = "sound"`）。**值必须一起取出来**：只报键名的话，下一轮
# 仍然只能知道"有没有这个键"，而不知道 SDK 真正看到的取值是多少——
# 那正是 2026-09-16 这次连查两轮的原因。
_PROP_LINE_RE = re.compile(r"^\t\t(?P<key>[^\t=]+?)\s*=\s*(?P<value>.*?)\s*$")


def _prop_entry(key: str, value: str) -> str:
    """属性行 → `key=value`。

    PA 会给字符串值加双引号（`device.description = "JaxNullSink"`）。这里剥掉成对的
    引号，让 `/status` 里的取值与 PA/我们写进 module 参数的字面量可比对；
    既没引号也不成对时原样保留，绝不改写取值内容（观测不得替现场作答）。
    """
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        # 只在"内部没有裸引号"时剥；否则原样给出，避免把转义过的值改错。
        if '"' not in inner:
            value = inner
    return f"{key}={value}"


def parse_device_details(text: str | None) -> list[dict] | None:
    """从 `pactl list sinks|sources`（长格式）里取每个设备的名称/端口/属性(key=value)/标志。

    为什么非要看**端口**和**属性取值**：TRTC 的 Linux ADM 在 `pulse_audio_context.cc`
    里枚举设备（原生字符串 `device.form_factor` / `device.description` /
    `GetDevicesList` / `RefreshPlayerDevices`），并且有按端口判定的分支
    （原生字符串 `sink device port is not same, but the active port and sink name
    is same`）。而我们用 `module-null-sink` 造出来的 sink 与真实声卡**不对称**：
    端口可能一个都没有，属性表也要么缺项、要么取值不同。
    **只列键名是不够的**——"有没有这个键"和"SDK 看到的取值是多少"是两件事，
    上一轮就是因为只列了键名而无法直接判定，才多花了一轮部署。
    所以这里输出 `key=value`（键名与取值一起），`None`（没清点成）与 `[]`
    （清点了但一个设备都没有）照例严格区分。
    """
    if text is None:
        return None
    out: list[dict] = []
    cur: dict | None = None
    section = ""
    for raw in text.splitlines():
        if _DEVICE_BLOCK_RE.match(raw):
            cur = {"name": "", "ports": [], "props": [], "flags": []}
            out.append(cur)
            section = ""
            continue
        if cur is None:
            continue
        line = raw.rstrip()
        if re.match(r"^\tPorts:\s*$", line):
            section = "ports"
            continue
        if re.match(r"^\tProperties:\s*$", line):
            section = "props"
            continue
        if re.match(r"^\t(Formats|Volume|Channel Map|Sample Specification):", line):
            section = ""
        if section == "ports":
            m = _PORT_LINE_RE.match(line)
            if m:
                cur["ports"].append(m.group("port"))
                continue
        if section == "props":
            m = _PROP_LINE_RE.match(line)
            if m:
                cur["props"].append(_prop_entry(m.group("key"), m.group("value")))
                continue
        m = re.match(r"^\tName:\s*(\S+)\s*$", line)
        if m:
            cur["name"] = m.group(1)
            section = ""
            continue
        m = re.match(r"^\tFlags:\s*(.*?)\s*$", line)
        if m:
            cur["flags"] = m.group(1).split()
            section = ""
            continue
        if line and not line.startswith("\t"):
            section = ""
    return out


_DEFAULT_SINK_RE = re.compile(r"^\s*Default Sink:\s*(\S+)\s*$", re.MULTILINE)
_DEFAULT_SOURCE_RE = re.compile(r"^\s*Default Source:\s*(\S+)\s*$", re.MULTILINE)


def parse_server_defaults(text: str | None) -> dict:
    """从 `pactl info` 文本里取默认 sink/source。

    为什么单拎出来：TRTC 的设备服务里有"跟随系统默认设备"这一步
    （原生字符串 `Failed to enable following default audio device` /
    `Audio device following default invalid, invalidate audio device direction`）。
    容器里只有一个我们自己造的 sink，**它是否被 PA 设为默认**是能不能被 SDK 选中的前提，
    而这一条此前完全没有观测面。文本缺失/字段缺失一律返回 None，绝不猜。
    """
    if not isinstance(text, str):
        return {"default_sink": None, "default_source": None}
    sink = _DEFAULT_SINK_RE.search(text)
    source = _DEFAULT_SOURCE_RE.search(text)
    return {
        "default_sink": sink.group(1) if sink else None,
        "default_source": source.group(1) if source else None,
    }


def parse_client_names(lines: list[str] | None) -> list[str] | None:
    """从 `pactl list short clients` 的行里取应用名（第 2 列）。

    这是「TRTC 到底连上了**哪一个** pulseaudio」的唯一直接证据：SDK 的 libpulse 客户端
    会把 `application.name` 设为 `liteav`（原生字符串 `pulseaudio.application.name.liteav`）。
    若客户端清单里**没有** liteav，说明它连的不是我们起的那一个实例 —— 那台服务端
    （自动拉起、默认配置）既没有硬件声卡也没有我们的 null-sink，设备表必然为空，
    而且**不会**打出 `pulse server connect failed`（这正是本次最难排除的分支）。
    """
    if lines is None:
        return None
    names: list[str] = []
    for line in lines:
        parts = [p for p in re.split(r"[\t]+", line.strip()) if p != ""]
        if len(parts) >= 2:
            names.append(parts[1])
        elif parts:
            names.append(parts[0])
    return names


def inspect_devices(*, env: dict[str, str], sink: str,
                    which=shutil.which, run=subprocess.run) -> dict:
    """清点音频设备：TRTC 的设备枚举必须有东西可拿，否则依旧 GetDevices wait。

    `probed` 是**必须看**的字段：pactl 不可用/调用失败时 sinks/sources 都是 None，
    这与"清点成功但设备为空"是两件完全不同的事，混为一谈会把一次工具故障
    误判成"音频子系统是哑的"（反之亦然，那更危险）。
    """
    sinks = _pactl_lines("sinks", env=env, which=which, run=run)
    sources = _pactl_lines("sources", env=env, which=which, run=run)
    clients = _pactl_lines("clients", env=env, which=which, run=run)
    defaults = parse_server_defaults(_pactl_info(env=env, which=which, run=run))
    # 长格式：每个 sink/source 的端口与属性 key=value。这是「SDK 为什么筛掉它」的唯一可判据。
    sink_details = parse_device_details(
        _pactl_list("sinks", env=env, which=which, run=run))
    source_details = parse_device_details(
        _pactl_list("sources", env=env, which=which, run=run))
    return {
        "pactl": (which(PACTL) or "") != "",
        "probed": sinks is not None and sources is not None,
        "sinks": sinks,
        "sources": sources,
        "clients": parse_client_names(clients),
        # monitor 与 virtual-source 名字都以 sink 名为前缀，故按前缀判定即可。
        "sink_present": bool(sinks) and any(sink in line for line in sinks),
        "source_present": bool(sources) and any(sink in line for line in sources),
        "default_sink": defaults["default_sink"],
        "default_source": defaults["default_source"],
        "sink_details": sink_details,
        "source_details": source_details,
    }


def summarize_devices(report: dict | None, *, error: str = "") -> dict:
    """清点结果 → `/status.audio.devices`（口径必须一眼可判，不给"0 台"留歧义）。

    `playout_ok` 的三态是要点：
      * True  —— 清点成功且 PA 里有 sink（**只**证明 PA 侧有设备）；
      * False —— 清点成功但 PA 里一个 sink 都没有：TRTC 的播放设备枚举必然为空，
                 远端音频帧不会回调、上行恒为 0（2026-09-16 事故的直接条件）；
      * None  —— 没清点成（pactl 缺失/失败）：**不知道**，不得当成 False，也不得当成 True。

    `sink_details` 是给"PA 有 sink 但 TRTC 说设备列表为空"这个分支用的：它把每个
    sink 的端口与属性 `key=value` 摊开，用于判定 SDK 是筛掉了"没有端口的 null-sink"
    还是筛掉了属性取值不达标的设备。**取值必须一起给出**：只给键名时，
    "有 device.form_factor 这个键"与"它的值是 speaker"是两件事，而 SDK 读到的是后者。
    """
    if not isinstance(report, dict):
        return {
            "probed": False, "sink_count": None, "source_count": None,
            "client_count": None, "client_names": None, "sink_present": False,
            "source_present": False, "default_sink": None, "default_source": None,
            "sink_details": None, "source_details": None,
            "playout_ok": None, "error": error,
        }
    sinks = report.get("sinks")
    sources = report.get("sources")
    clients = report.get("clients")
    probed = bool(report.get("probed"))
    playout_ok = (bool(report.get("sink_present")) if probed else None)
    return {
        "probed": probed,
        "sink_count": len(sinks) if isinstance(sinks, list) else None,
        "source_count": len(sources) if isinstance(sources, list) else None,
        "client_count": len(clients) if isinstance(clients, list) else None,
        "client_names": clients if isinstance(clients, list) else None,
        "sink_present": bool(report.get("sink_present")),
        "source_present": bool(report.get("source_present")),
        "default_sink": report.get("default_sink"),
        "default_source": report.get("default_source"),
        "sink_details": report.get("sink_details"),
        "source_details": report.get("source_details"),
        "playout_ok": playout_ok,
        "error": error,
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
