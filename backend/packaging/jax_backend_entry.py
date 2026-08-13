"""jax-backend.exe 生产入口（PyInstaller 打包目标）。

与 ``backend/run.py``（开发入口）的区别：
- 对象导入 ``app.main:app``（非字符串），单进程、``reload=False``——冻结环境不支持 reload 的
  multiprocessing spawn 子进程。
- TLS 逻辑与 run.py 一致：配置了 ``TLS_CERTFILE``/``TLS_KEYFILE`` 即启用 HTTPS/WSS；
  生产模式（``VOICE_PRODUCTION=true``）缺 TLS 或证书缺失则 fail-closed 拒绝启动。

用法（打包后）：
    jax-backend.exe --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os
import sys

# 冻结态 windowed（console=False，PE 子系统=WINDOWS_GUI）下无控制台，
# PyInstaller 会把 sys.stdout/stderr 置为 None；任何 print() 或 logging.StreamHandler(sys.stdout)
# 都会抛 AttributeError。兜底重定向到 devnull，保证「无重定向直接启动」也不崩（对齐服务层 pythonw 无窗口）。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import argparse
import logging
from pathlib import Path

import uvicorn

from app.config import PROJECT_ROOT, config

log = logging.getLogger(__name__)


def _resolve(path: str) -> Path:
    """相对路径按项目根（PROJECT_ROOT）解析为绝对路径，确保任意 cwd 启动都正确。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _tls_kwargs() -> dict:
    settings = config.settings
    certfile = settings.tls_certfile
    keyfile = settings.tls_keyfile
    if not certfile or not keyfile:
        if settings.voice_production:
            raise RuntimeError("生产模式必须配置 TLS_CERTFILE / TLS_KEYFILE")
        log.warning("TLS 未配置，以明文 HTTP/WS 启动（仅限本地开发）")
        return {}
    cert_path = _resolve(certfile)
    key_path = _resolve(keyfile)
    if not cert_path.exists() or not key_path.exists():
        raise FileNotFoundError(f"TLS 证书缺失: {cert_path} / {key_path}")
    log.info("启用 HTTPS/WSS：cert=%s key=%s", cert_path, key_path)
    return {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="贾克斯后端（jax-backend）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=config.settings.backend_port)
    args = parser.parse_args()

    from app.main import app  # 延迟导入：确保 config 就绪后再装配 app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=False,
        **_tls_kwargs(),
    )


if __name__ == "__main__":
    main()
