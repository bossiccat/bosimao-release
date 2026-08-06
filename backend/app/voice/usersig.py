"""TRTC UserSig 签发（TLSSigAPIv2 官方算法，纯 Python，不依赖腾讯云 SDK 大包）

实现基准：腾讯云官方 tls-sig-api-v2-python（GitHub tencentyun/tls-sig-api-v2-python，
文档 https://cloud.tencent.com/document/product/647/17275）。

算法要点（与 PC-INTEGRATION.md 附录 A.1 的简化草稿不同，以官方实现为准）：
1. 被签名的原文是「TLS.identifier/TLS.sdkappid/TLS.time/TLS.expire 四行 key:value 拼接」，
   不是整个 JSON dict；
2. sig = base64( HMAC-SHA256(secret_key, 原文) )；
3. 整体 = json.dumps({TLS.ver, TLS.identifier, TLS.sdkappid, TLS.expire, TLS.time, TLS.sig})；
4. 先 zlib.compress，再做自定义 base64：+→*、/→-、=→_（不是标准 base64，也不是标准 urlsafe）。

SecretKey 只从 .env 注入（Settings.trtc_secretkey），本模块不落任何密钥默认值。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import zlib
from typing import Any

TLS_VERSION = "2.0"


def _base64_encode_url(data: bytes) -> str:
    """官方 base64_encode_url：+→*、/→-、=→_"""
    s = base64.b64encode(data).decode("ascii")
    return s.replace("+", "*").replace("/", "-").replace("=", "_")


def _base64_decode_url(data: str) -> bytes:
    """官方 base64_decode_url：*→+、-→/、_→= """
    s = data.replace("*", "+").replace("-", "/").replace("_", "=")
    return base64.b64decode(s)


def gen_user_sig(sdk_app_id: int, secret_key: str, user_id: str, expire_s: int = 600) -> str:
    """生成 TRTC UserSig（TLSSigAPIv2.genUserSig 的等价纯 Python 实现）

    Args:
        sdk_app_id: TRTC SDKAppID（int）
        secret_key: TRTC 控制台 SecretKey（仅 .env，禁入库/日志）
        user_id: 进房 userId（≤32 字节，字母/数字/下划线/连字符）
        expire_s: 有效期秒（本项目契约 ≤600s）
    """
    if not secret_key:
        raise ValueError("secret_key 为空：TRTC_SECRETKEY 未配置")
    curr_time = int(time.time())

    raw_to_sign = (
        f"TLS.identifier:{user_id}\n"
        f"TLS.sdkappid:{int(sdk_app_id)}\n"
        f"TLS.time:{curr_time}\n"
        f"TLS.expire:{int(expire_s)}\n"
    )
    sig = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), raw_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")

    payload: dict[str, Any] = {
        "TLS.ver": TLS_VERSION,
        "TLS.identifier": str(user_id),
        "TLS.sdkappid": int(sdk_app_id),
        "TLS.expire": int(expire_s),
        "TLS.time": int(curr_time),
        "TLS.sig": sig,
    }
    raw_sig = json.dumps(payload)  # 官方用默认分隔符（含空格），保持字节一致
    compressed = zlib.compress(raw_sig.encode("utf-8"))
    return _base64_encode_url(compressed)


def parse_user_sig(user_sig: str) -> dict[str, Any]:
    """解包 UserSig（zlib 解压 + 反序列化），用于测试/调试，禁止在正式路径使用"""
    raw = zlib.decompress(_base64_decode_url(user_sig))
    return json.loads(raw.decode("utf-8"))


def user_sig_expire_ok(user_sig: str, max_expire_s: int = 600) -> bool:
    """校验 userSig 有效期字段（过期时间点 - 签发时间点 ≤ max_expire_s）"""
    payload = parse_user_sig(user_sig)
    return int(payload["TLS.expire"]) <= max_expire_s
