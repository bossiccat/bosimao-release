"""llama.cpp-omni HTTP 客户端（独立子进程 :19080）

API 参考（架构师已联网核实）：
- GET  /health
- POST /v1/stream/prefill    （img_path_prefix / audio_path_prefix / cnt）
- POST /v1/stream/decode     （stream=true）
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)


class ModelServerError(RuntimeError):
    pass


class LlamaOmniClient:
    """模型服务客户端（单实例互斥由 orchestrator 的 asyncio 锁保证）"""

    def __init__(self, settings: Settings, timeout: float = 30.0) -> None:
        self.base_url = f"http://{settings.model_server_host}:{settings.model_server_port}"
        self.timeout = timeout
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        try:
            resp = await self._client.get("/health")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            raise ModelServerError(f"模型服务不可达: {e}") from e

    async def vision_analyze(self, image_path: Path, prompt: str, max_tokens: int = 256) -> str:
        """单帧视觉判定：prefill(图片) → decode(生成 JSON)"""
        if not image_path.exists():
            raise ModelServerError(f"截图不存在: {image_path}")

        start = time.perf_counter()
        try:
            # 1) 视觉预填充
            prefill = await self._client.post(
                "/v1/stream/prefill",
                json={"img_path_prefix": str(image_path), "cnt": 1},
            )
            prefill.raise_for_status()
            prefill_ms = (time.perf_counter() - start) * 1000

            # 2) 生成（流式解码）
            decode = await self._client.post(
                "/v1/stream/decode",
                json={
                    "stream": True,
                    "max_tokens": max_tokens,
                    "prompt": prompt,
                },
            )
            decode.raise_for_status()
            text = decode.text
        except httpx.HTTPError as e:
            raise ModelServerError(f"视觉推理失败: {e}") from e

        total_ms = (time.perf_counter() - start) * 1000
        logger.info("vision_analyze ok: prefill=%dms total=%dms tokens=%d",
                    int(prefill_ms), int(total_ms), len(text))
        return text

    async def chat(self, prompt: str, max_tokens: int = 512) -> str:
        """纯文本对话（任务建议/问答用，V1 保留）"""
        try:
            decode = await self._client.post(
                "/v1/stream/decode",
                json={"stream": True, "max_tokens": max_tokens, "prompt": prompt},
            )
            decode.raise_for_status()
            return decode.text
        except httpx.HTTPError as e:
            raise ModelServerError(f"对话调用失败: {e}") from e
