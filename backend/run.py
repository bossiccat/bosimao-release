"""开发启动入口：uvicorn app.main:app --port 8000"""
import uvicorn

from app.config import config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=config.settings.backend_port,
        reload=True,
    )
