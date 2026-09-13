"""云端手机模拟器的设备 provisioning：走**真实配对流程**取得设备凭证。

为什么必须走真实配对
--------------------
模拟器代表一台"新手机"。服务端对 `/api/v1/voice/session` 的要求与真机完全一致：
设备必须先由 owner 签发 pairing_code、再 register 换取 (device_id, credential_secret)，
并以 `f"{device_id}.{credential_secret}"` 作为 Bearer。任何"绕过配对"的捷径都会让这次
模拟失去意义——它验证的就不再是真机路径。因此本模块复刻真机 provisioning 的三步：

    owner  PATCH /api/v1/privacy/cloud_processing   {"enabled": true}
    owner  POST  /api/v1/voice/devices/pairing-code  → pairing_code
    device POST  /api/v1/voice/devices/register      → device_id + credential_secret

安全约定（对应容器"凭证只来自环境变量、不落明文"的硬性要求）
----------------------------------------------------------
- 凭证只由环境变量注入，**绝不**写入日志、异常 message 或状态端点；
- `SimProvisionError` 只携带「阶段名:错误码」，`str(exc)` 形如 "privacy:40301"；
- 仅用标准库（与 supervisor.py 现有的 urllib 方式一致），不引入新依赖。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

# URL 段名已按服务端路由核实：
#   backend/app/api/routes_voice_privacy.py:21-27  SETTING_PATH_KEYS 的 URL 段就是
#   "cloud_processing"（映射到 cloud_processing_enabled）；body 由 :31-32 定义为
#   {"enabled": bool}。其余两条见 backend/app/api/routes_voice_devices.py:87 / :107。
PRIVACY_CLOUD_PROCESSING_PATH = "/api/v1/privacy/cloud_processing"
PAIRING_CODE_PATH = "/api/v1/voice/devices/pairing-code"
REGISTER_PATH = "/api/v1/voice/devices/register"

DEFAULT_DEVICE_NAME = "jax-sim-phone"
DEFAULT_TIMEOUT_S = 20


@dataclass(frozen=True)
class SimDevice:
    """注册返回的设备身份。credential_token 只走内存/env，不落盘、不进日志。"""

    device_id: str
    credential_token: str  # f"{device_id}.{credential_secret}"
    expires_at: str


class SimProvisionError(RuntimeError):
    """provisioning 失败。只含阶段名与错误码，**绝不**含任何凭证。"""

    def __init__(self, stage: str, code) -> None:
        self.stage = stage
        self.code = code
        # 注意：此处只拼接 stage 与 code，绝不把 token/pairing_code/secret 放进来。
        super().__init__(f"{stage}:{code}")


def new_nonce() -> str:
    """一次性请求 nonce：64 位小写 hex（uuid4().hex 本身是 32 位，两次拼接）。"""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _decode(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 非 JSON 响应按空体处理，交由调用方判码
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _request(
    method: str,
    url: str,
    payload: dict,
    *,
    owner_credential: str = "",
    device_token: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, dict]:
    """发一次 JSON 请求，返回 (status, body)。

    - 显式设置 Content-Type；按需带 Authorization（owner 优先于 device）；
    - 始终带一个全新的 X-Request-Nonce（服务端 nonce 一次性消费）；
    - HTTPError 也是响应：读出 body 里的业务码后返回，**不在此处抛携带鉴权信息的异常**。
    """
    headers = {
        "Content-Type": "application/json",
        "X-Request-Nonce": new_nonce(),
    }
    if owner_credential:
        headers["Authorization"] = f"Bearer {owner_credential}"
    elif device_token:
        headers["Authorization"] = f"Bearer {device_token}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _raise_if_bad(stage: str, status: int, body: dict) -> None:
    code = body.get("code")
    if not (200 <= status < 300) or code != 0:
        raise SimProvisionError(stage, code if code is not None else status)


def _data_or_raise(stage: str, body: dict) -> dict:
    data = body.get("data")
    if not isinstance(data, dict):
        raise SimProvisionError(stage, "MALFORMED_RESPONSE")
    return data


def enable_cloud_processing(
    base_url: str, owner_credential: str, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> None:
    """打开服务端 cloud_processing 开关。

    这是 /session 的前置门禁：`routes_voice_security_context.py:41-47` 的
    cloud_processing_gate 在开关不为真时直接返回 40301。模拟器若不开它就会在
    第二步（session）即失败，从而测不出真实链路。
    """
    status, body = _request(
        "PATCH",
        _url(base_url, PRIVACY_CLOUD_PROCESSING_PATH),
        {"enabled": True},
        owner_credential=owner_credential,
        timeout_s=timeout_s,
    )
    _raise_if_bad("privacy", status, body)


def provision_sim_device(
    base_url: str,
    owner_credential: str,
    *,
    device_name: str = DEFAULT_DEVICE_NAME,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SimDevice:
    """按真机顺序完成 provisioning：privacy 门禁 → pairing-code → register。"""
    enable_cloud_processing(base_url, owner_credential, timeout_s=timeout_s)

    status, body = _request(
        "POST",
        _url(base_url, PAIRING_CODE_PATH),
        {"platform": "android", "device_name_hint": device_name},
        owner_credential=owner_credential,
        timeout_s=timeout_s,
    )
    _raise_if_bad("pairing_code", status, body)
    pairing_code = _data_or_raise("pairing_code", body).get("pairing_code")
    if not pairing_code:
        raise SimProvisionError("pairing_code", "MALFORMED_RESPONSE")

    # register 以 pairing_code 为 bootstrap 主体，**不带** owner Authorization。
    status, body = _request(
        "POST",
        _url(base_url, REGISTER_PATH),
        {"pairing_code": pairing_code, "device_name": device_name, "platform": "android"},
        timeout_s=timeout_s,
    )
    _raise_if_bad("register", status, body)
    data = _data_or_raise("register", body)
    device_id = data.get("device_id")
    credential_secret = data.get("credential_secret")
    if not device_id or not credential_secret:
        raise SimProvisionError("register", "MALFORMED_RESPONSE")
    return SimDevice(
        device_id=device_id,
        credential_token=f"{device_id}.{credential_secret}",
        expires_at=str(data.get("expires_at", "")),
    )


def resolve_sim_device(
    *,
    base_url: str,
    explicit_token: str = "",
    owner_credential: str = "",
    device_name: str = DEFAULT_DEVICE_NAME,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SimDevice:
    """决定这次模拟用哪套设备凭证：

    1. 显式 token（形如 `<device_id>.<credential_secret>`）优先——不再发任何 HTTP，
       让运维/门禁可复用一台已注册设备，避免每次重启都新建设备；
    2. 否则用 owner 凭证现走一遍真实配对；
    3. 两者都缺 → config 阶段失败（明确指名缺哪个 env，而不是静默降级）。
    """
    if explicit_token and "." in explicit_token:
        device_id = explicit_token.split(".", 1)[0]
        return SimDevice(device_id=device_id, credential_token=explicit_token, expires_at="")
    if owner_credential:
        return provision_sim_device(
            base_url, owner_credential, device_name=device_name, timeout_s=timeout_s
        )
    raise SimProvisionError("config", "SIM_DEVICE_CREDENTIAL_MISSING")
