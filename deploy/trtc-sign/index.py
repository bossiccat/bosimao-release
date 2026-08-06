"""trtc-sign 云函数入口（腾讯云 SCF / CloudBase，Python 3，API Gateway HTTP 触发器）

路由：
- POST /api/v1/voice/session            手机 KWS 唤醒 → 签手机凭证
- GET  /api/v1/voice/session/pending     PC 轮询会话意图
- POST /api/v1/voice/session/sign        PC 取自身 userSig 进同房

统一响应（与 PC 后端契约一致）：{code, data, message}
- code=0 成功；40001 device_id 非法；40400 无会话意图；50300 凭据未配置；50000 内部错误

部署：
1. 控制台新建云函数 trtc-sign（runtime=Python 3.x，本目录为函数代码根）
2. 环境变量：TRTC_SDKAPPID / TRTC_SECRETKEY / TRTC_ROOM_PREFIX（可选 TRTC_DEVICE_WHITELIST）
3. API Gateway 触发器：路径 /api/v1/voice/session，方法 ANY（或 POST+GET），开启 CORS
   详见 README.md / serverless.yaml
"""
from __future__ import annotations

import json
import logging
import sys

# SCF 函数根为扁平目录（非 package）：绝对导入（config/signing/usersig 与 index.py 同级）
from config import TrtcSignConfig
from signing import (
    ConfigMissingError,
    InvalidDeviceIdError,
    InvalidUserIdError,
    SignError,
    TrtcSignService,
    UnknownDeviceError,
)

logger = logging.getLogger(__name__)

# 统一响应码
OK = 0
ERR_DEVICE = 40001
ERR_USER = 40002
ERR_UNKNOWN_DEVICE = 40400
ERR_MISSING_CFG = 50300
ERR_INTERNAL = 50000

# CORS（手机 App 直调公网网关；生产可按需收敛为固定域名）
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Device-Token",
}

_svc: TrtcSignService | None = None


def _get_service() -> TrtcSignService:
    """惰性装配（冷启动优化：首次请求初始化，后续复用）"""
    global _svc
    if _svc is None:
        _svc = TrtcSignService(TrtcSignConfig())
    return _svc


def _json_response(status_code: int, body: dict) -> dict:
    """组装 API Gateway 响应对象"""
    headers = {"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS}
    return {
        "isBase64Encoded": False,
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _err(status_code: int, code: int, message: str) -> dict:
    return _json_response(status_code, {"code": code, "data": None, "message": message})


def _ok(data: dict | list | None) -> dict:
    return _json_response(200, {"code": OK, "data": data, "message": ""})


def _parse_body(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _route(http_method: str, path: str, query: dict, body: dict) -> dict:
    svc = _get_service()
    try:
        if path == "/api/v1/voice/session":
            if http_method != "POST":
                return _err(405, 40500, "Method not allowed")
            device_id = str(body.get("device_id") or "").strip()
            return _ok(svc.issue(device_id))
        if path == "/api/v1/voice/session/pending":
            if http_method != "GET":
                return _err(405, 40500, "Method not allowed")
            device_id = str(query.get("device_id") or "").strip()
            if not device_id:
                return _err(400, ERR_DEVICE, "device_id 不能为空")
            intent = svc.pending(device_id)
            return _ok(intent if intent is not None else None)
        if path == "/api/v1/voice/session/sign":
            if http_method != "POST":
                return _err(405, 40500, "Method not allowed")
            device_id = str(body.get("device_id") or "").strip()
            user_id = str(body.get("user_id") or "jax-pc-sidecar").strip()
            return _ok(svc.sign_for_sidecar(device_id, user_id=user_id))
        return _err(404, 40400, "Not found")
    except ConfigMissingError as e:
        return _err(503, ERR_MISSING_CFG, str(e))
    except InvalidDeviceIdError as e:
        return _err(400, ERR_DEVICE, str(e))
    except InvalidUserIdError as e:
        return _err(400, ERR_USER, str(e))
    except UnknownDeviceError as e:
        return _err(404, ERR_UNKNOWN_DEVICE, str(e))
    except SignError as e:
        return _err(400, ERR_DEVICE, str(e))
    except Exception:  # noqa: BLE001 - 兜底，不泄露内部细节
        logger.exception("trtc-sign unhandled error path=%s method=%s", path, http_method)
        return _err(500, ERR_INTERNAL, "Internal server error")


def main_handler(event: dict, context) -> dict:
    """SCF 入口（API Gateway 触发器）"""
    if not isinstance(event, dict):
        return _err(500, ERR_INTERNAL, "Invalid event")
    http_method = event.get("httpMethod") or "GET"
    path = event.get("path") or "/"
    query = event.get("queryStringParameters") or event.get("queryString") or {}
    if not isinstance(query, dict):
        query = {}

    # CORS 预检
    if http_method == "OPTIONS":
        return {
            "isBase64Encoded": False,
            "statusCode": 204,
            "headers": _CORS_HEADERS,
            "body": "",
        }

    body = {}
    if http_method == "POST":
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64

            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
        body = _parse_body(raw)
    return _route(http_method, path, query, body)


# 本地开发自检：python index.py（无 SCF 事件时模拟请求）
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    demo = {
        "httpMethod": "POST",
        "path": "/api/v1/voice/session",
        "queryStringParameters": {},
        "body": json.dumps({"device_id": "jax-demo-001"}),
        "isBase64Encoded": False,
    }
    sys.stdout.write(json.dumps(main_handler(demo, None), ensure_ascii=False) + "\n")
