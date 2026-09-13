"""运行时把 PEM 证书材料从环境变量落成受限权限临时文件。

为什么存在
----------
jax-voice-bridge 容器内的 rtc_bridge 必须用 mTLS 调云端控制面完成 hello 兑付，
兑付失败是 fail-closed 终局的。但镜像里**绝不能烘入 client.key**——私钥一旦进入
镜像层，就会被任何能拉取镜像的人读到（见 supervisor.py 顶部「凭据只来自环境变量」
与 CANONICAL_SOURCE.json 的 notes）。

因此约定：密钥与客户端证书一律由 CloudRun 以环境变量注入 PEM 文本
（RTC_BRIDGE_CLIENT_CERT_PEM / RTC_BRIDGE_CLIENT_KEY_PEM /
RTC_BRIDGE_CONTROL_PLANE_CA_PEM）。supervisor 在拉起 rtc_bridge **之前**调用
本模块，把它们落成 0o600 的临时文件，再把 rtc_bridge 实际读取的
RTC_BRIDGE_*_FILE 指向这些文件。

硬约束（不要违反）
------------------
PEM 内容与私钥**绝不**写入日志、异常文本或任何可观测输出；只允许出现在进程
内存与受限权限文件中。因此 TlsMaterialError 的消息只列缺失的**键名**，
不含任何键值。仅依赖标准库，纯函数、可单测。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 输入：PEM 文本（由 CloudRun 环境变量注入，绝不烘进镜像）
CLIENT_CERT_PEM = "RTC_BRIDGE_CLIENT_CERT_PEM"
CLIENT_KEY_PEM = "RTC_BRIDGE_CLIENT_KEY_PEM"
CONTROL_PLANE_CA_PEM = "RTC_BRIDGE_CONTROL_PLANE_CA_PEM"

# 输出：rtc_bridge 实际读取的文件路径键（与 backend/rtc_bridge/config.py:118-120 对齐）
CLIENT_CERT_FILE = "RTC_BRIDGE_CLIENT_CERT_FILE"
CLIENT_KEY_FILE = "RTC_BRIDGE_CLIENT_KEY_FILE"
CONTROL_PLANE_CA_FILE = "RTC_BRIDGE_CONTROL_PLANE_CA_FILE"

# (输入 PEM 键, 落盘文件名, 输出 *_FILE 键)
_SPEC = (
    (CLIENT_CERT_PEM, "client.crt", CLIENT_CERT_FILE),
    (CLIENT_KEY_PEM, "client.key", CLIENT_KEY_FILE),
    (CONTROL_PLANE_CA_PEM, "ca.crt", CONTROL_PLANE_CA_FILE),
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600


class TlsMaterialError(RuntimeError):
    """TLS 材料无法落盘（部分配置或目标目录不可写）。

    消息不含任何 PEM 内容——只列缺失的键名或目标目录。
    """


def _write_atomic(path: Path, data: str) -> None:
    """先写同目录临时文件，chmod 后 os.replace 原子替换，避免读者看到半截内容。"""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tls-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def materialize_tls_files(target_dir, env) -> dict[str, str]:
    """把 PEM 环境变量落成受限权限文件，返回 ``*_FILE`` 环境变量映射。

    CA 有**两条**合法来源，二者其一即可：
      1. ``RTC_BRIDGE_CONTROL_PLANE_CA_PEM``：PEM 文本走环境变量（本函数落盘）；
      2. ``RTC_BRIDGE_CONTROL_PLANE_CA_FILE``：直接指向**镜像内已存在**的 CA 文件
         （例如 python:3.11-slim 的 ``/etc/ssl/certs/ca-certificates.crt``）。
         这不是绕过校验，而是避免把整份 CA bundle 塞进环境变量——实测 CloudRun
         对 EnvParam 有 **5KB 上限**，而公共 CA bundle 就有 11.9KB，方案 1 会直接
         被平台拒绝（``环境变量长度超长，最长支持5kb``）。

    fail-closed 四条：
      a. 三个 PEM 全空 → 返回 ``{}``（表示「未配置」），且**不创建任何文件**，
         由下游 rtc_bridge 既有的 fail-closed 语义处理；绝不伪造证书。
      b. 缺 client cert / client key → 抛 ``TlsMaterialError``，消息只列缺失键名。
      c. CA 两条来源皆空 → 抛 ``TlsMaterialError``（同上）。
      d. 目标目录不可写 → 抛 ``TlsMaterialError``。
    """
    # 判空用 strip（环境注入可能带首尾空白/换行），但落盘写**原值**——
    # PEM 末尾换行需原样保留，避免任何解析器对「缺终止换行」的容忍度差异。
    raw_cert = env.get(CLIENT_CERT_PEM) or ""
    raw_key = env.get(CLIENT_KEY_PEM) or ""
    raw_ca = env.get(CONTROL_PLANE_CA_PEM) or ""
    external_ca = (env.get(CONTROL_PLANE_CA_FILE) or "").strip()

    if not any(v.strip() for v in (raw_cert, raw_key, raw_ca)):
        return {}

    missing: list[str] = []
    if not raw_cert.strip():
        missing.append(CLIENT_CERT_PEM)
    if not raw_key.strip():
        missing.append(CLIENT_KEY_PEM)
    if not raw_ca.strip() and not external_ca:
        missing.append(f"{CONTROL_PLANE_CA_PEM} 或 {CONTROL_PLANE_CA_FILE}")
    if missing:
        raise TlsMaterialError(
            "incomplete TLS material configuration; missing: " + ", ".join(missing)
        )

    entries = [
        ("client.crt", CLIENT_CERT_FILE, raw_cert, raw_cert.strip()),
        ("client.key", CLIENT_KEY_FILE, raw_key, raw_key.strip()),
    ]
    if raw_ca.strip():
        entries.append(("ca.crt", CONTROL_PLANE_CA_FILE, raw_ca, raw_ca.strip()))
    # CA 走镜像内既存文件时不落盘、也不改写 RTC_BRIDGE_CONTROL_PLANE_CA_FILE。

    directory = Path(target_dir)
    try:
        # mkdir 的 mode 会被 umask 削弱，故再显式 chmod 一次。
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, _DIR_MODE)
        result: dict[str, str] = {}
        for filename, env_name, raw, _ in entries:
            final = directory / filename
            _write_atomic(final, raw)
            result[env_name] = str(final.resolve())
        return result
    except OSError as exc:
        raise TlsMaterialError(
            f"cannot materialize TLS material into {directory} ({type(exc).__name__})"
        ) from exc
