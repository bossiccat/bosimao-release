"""后端崩溃/未捕获异常落盘收集器（阶段 E-1）

职责：
- 把未捕获异常的完整 traceback 落盘到 ``logs/backend-crash/``（文件名带时间戳）
- 落盘前对敏感字段做掩码（token/credential/secret/key/password/bearer/签名等），
  绝不把明文 secret 写进崩溃日志（对齐 ADR-018「本地最小隐私数据」精神）
- 不引入 sentry 等外部依赖，纯本地落盘

落盘内容（JSON）：时间戳 + 版本号 + 异常类型 + 脱敏后的异常消息 + 脱敏后的完整 traceback
+ 请求上下文（HTTP 场景记录 method/path，不含 query 与 header）。

设计约束：
- 崩溃收集器自身必须 fail-safe：任何一步失败都不得再抛异常掩盖原始崩溃。
- 脱敏采用「敏感键名子串 + 常见秘密格式」两路掩码，优先宁多勿漏。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .._frozen_paths import project_root

DEFAULT_VERSION = "1.0.0"

_CRASH_DIR = project_root() / "logs" / "backend-crash"

# 掩码标记刻意不用方括号：方括号会被 key=value 的无引号值字符类截断，
# 导致二次匹配产生 `...]]` 残留。用尖括号可保证对各类模式幂等。
_REDACTION = "<REDACTED>"

_log = logging.getLogger("crash_reporter")

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

# 敏感键名子串：key 名命中任一项即对其后紧随的值做掩码。
# 刻意不含裸 "key" 子串（会误伤 monkey/hockey/keyboard）；裸 "key" 单独按词边界处理。
_SENSITIVE_KEY_PATTERNS = (
    r"token",
    r"secret",
    r"credential",
    r"password",
    r"passphrase",
    r"api[_-]?key",
    r"apikey",
    r"access[_-]?key",
    r"private[_-]?key",
    r"public[_-]?key",
    r"session[_-]?key",
    r"encryption[_-]?key",
    r"signing[_-]?key",
    r"authorization",
    r"bearer",
    r"signature",
    r"user[_-]?sig",
    r"usersig",
    r"nonce",
    r"webhook",
    r"pairing[_-]?code",
)

# 键名匹配：敏感子串 / *_key·*-key 后缀 / 裸 "key"（词边界）
_KEY_NAME = (
    r"[\w.\-]*(?:"
    + "|".join(_SENSITIVE_KEY_PATTERNS)
    + r")[\w.\-]*"
    r"|[\w.\-]*[_-]key"
    r"|\bkey\b"
)

# key[:=]value，值可为引号包裹或无引号（到空白/逗号/分号/右括号/引号为止）
# 注意 _KEY_NAME 内含 `|` 分支，必须用 (?:...) 包裹，否则优先级会拆散整条正则
_KEY_VALUE_RE = re.compile(
    rf'(?i)(?P<prefix>[\'"]?(?:{_KEY_NAME})[\'"]?\s*[:=]\s*)'
    rf'(?P<value>"[^"]*"|\'[^\']*\'|[^\s,;}}\]\'"]+)'
)

# 常见秘密格式（独立于 key=value，捕获裸值）
_SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{6,}")
# "Bearer <token>" 整体掩码（含 scheme 标记），避免遗留 "Bearer " 前缀被 key=value 二次截断
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
# 裸敏感词后紧跟的 token 形值（≥8 字符且含数字），兜底 "token abc123..." 这类散写
_NAKED_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:token|secret|password|credential|apikey|api[_-]?key)\b\s+)"
    r"(?P<value>(?=[A-Za-z0-9._~+/=\-]*[0-9])[A-Za-z0-9._~+/=\-]{8,})"
)


def redact_text(text: str) -> str:
    """对任意文本做敏感值掩码；未知/不含敏感信息时原样返回。

    顺序：先掩码「裸秘密格式」（bearer / sk- / JWT / 裸 token 值），
    再掩码 key=value。这样 ``Authorization: Bearer <token>`` 会整体先被
    bearer 规则吞掉，不会被 key=value 把 ``Bearer`` 当作值提前截断。
    """
    if not text:
        return text
    out = _BEARER_RE.sub(_REDACTION, text)
    out = _SK_RE.sub(_REDACTION, out)
    out = _JWT_RE.sub(_REDACTION, out)
    out = _NAKED_SECRET_RE.sub(
        lambda m: m.group("prefix") + _REDACTION, out
    )
    out = _KEY_VALUE_RE.sub(
        lambda m: m.group("prefix") + _REDACTION, out
    )
    return out


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def report_crash(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb,
    *,
    request: dict | None = None,
    version: str = DEFAULT_VERSION,
    crash_dir: Path | None = None,
) -> Path | None:
    """把一次未捕获异常落盘为 JSON；失败时静默返回 None（不掩盖原始崩溃）。

    request 仅接受 method/path 两个纯文本键，不记录 query 与 header。
    """
    try:
        exc_type_name = getattr(exc_type, "__name__", str(exc_type))
        message = redact_text(str(exc_value) or "")
        tb_text = redact_text(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )
        payload: dict = {
            "ts": _now_iso(),
            "version": version,
            "exc_type": exc_type_name,
            "exc_message": message,
            "traceback": tb_text,
            "thread": threading.current_thread().name,
        }
        if request:
            payload["request"] = {
                "method": str(request.get("method", "")),
                "path": redact_text(str(request.get("path", ""))),
            }

        target_dir = crash_dir if crash_dir is not None else _CRASH_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ns = time.time_ns() % 1_000_000_000
        path = target_dir / f"crash-{stamp}-{ns:09d}.json"

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with _write_lock:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001 - 崩溃收集器必须 fail-safe
        try:
            _log.exception("crash report write failed")
        except Exception:  # noqa: BLE001
            pass
        return None


# ---------------------------------------------------------------------------
# 全局钩子 + FastAPI 异常处理器
# ---------------------------------------------------------------------------

_original_excepthook = sys.excepthook
_original_thread_excepthook = threading.excepthook
_hooks_installed = False


def install_crash_hooks(version: str = DEFAULT_VERSION) -> None:
    """安装主线程/子线程的未捕获异常钩子；幂等（只装一次）。"""
    global _hooks_installed
    if _hooks_installed:
        return

    def _excepthook(exc_type, exc_value, exc_tb):
        try:
            report_crash(exc_type, exc_value, exc_tb, version=version)
        except Exception:  # noqa: BLE001
            pass
        _original_excepthook(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args):
        try:
            report_crash(
                args.exc_type, args.exc_value, args.exc_traceback, version=version
            )
        except Exception:  # noqa: BLE001
            pass
        if _original_thread_excepthook is not None:
            _original_thread_excepthook(args)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    _hooks_installed = True


def build_fastapi_exception_handler(version: str = DEFAULT_VERSION):
    """返回 FastAPI 全局异常处理器（捕获路由内未 try/except 的异常）。

    记录 request method/path 后落盘，并回统一 500 响应（code=50000）。
    """

    async def _handler(request, exc: BaseException):
        try:
            report_crash(
                type(exc),
                exc,
                exc.__traceback__,
                request={"method": request.method, "path": request.url.path},
                version=version,
            )
        except Exception:  # noqa: BLE001
            pass
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=500,
            content={"code": 50000, "data": None, "message": "Internal server error"},
        )

    return _handler
