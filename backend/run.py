"""开发启动入口：uvicorn app.main:app --port 8000

2026-08-13 TLS 底座：配置 TLS_CERTFILE / TLS_KEYFILE 时启用 HTTPS/WSS，
否则保持明文（本地开发）。生产（VOICE_PRODUCTION=true）必须启用 TLS。
"""
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
    """按配置返回 uvicorn 的 SSL 参数；未配置证书时返回空（明文）。

    fail-closed：生产模式未配置 TLS 或证书文件缺失时拒绝启动。
    """
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


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=config.settings.backend_port,
        reload=True,
        **_tls_kwargs(),
    )
