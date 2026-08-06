"""llama.cpp-omni HTTP 客户端（独立子进程 :19080）

正确调用序列（PoC B1 实测，docs/poc/POC-001-model-vision.md §SSE 格式验证）：
① POST /v1/stream/omni_init    {media_type:2, use_tts:false, duplex_mode:false, model_dir}
② POST /v1/stream/prefill      图片 {img_path_prefix, cnt} → 文本 {text, cnt}
③ POST /v1/stream/decode       {stream:true, round_idx, length_penalty, use_tts:false}
   响应为 SSE：data:{"content":...} 逐块 + data:[DONE] 终止（sse.py 解析）

错误三分类（backend-llama-client-spec §4.1，统一聚合出口 ModelServerError）：
- ModelNetworkError：httpx.TransportError（连接/超时/断流）→ 整轮重试 1 次
- SseProtocolError：非 SSE 响应 / 畸形行 / 流内缺 [DONE] → 不可重试
- ModelError：HTTP 4xx/5xx / SSE {"error":...} / 输入不合法 → 不可重试
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from .sse import SseProtocolError as SseParseError
from .sse import iter_sse_chunks

logger = logging.getLogger(__name__)


class ModelServerError(RuntimeError):
    """模型服务错误基类（统一聚合出口）"""


class ModelNetworkError(ModelServerError):
    """网络类错误（连接拒绝/超时/断流）——可重试"""


class SseProtocolError(ModelServerError):
    """SSE 协议错误（非 SSE/畸形行/缺 [DONE]）——不可重试"""


class ModelError(ModelServerError):
    """模型服务错误（HTTP 4xx/5xx/SSE error）——不可重试"""


class LlamaOmniClient:
    """模型服务客户端（单实例互斥由 orchestrator 的 asyncio 锁保证）"""

    def __init__(
        self,
        settings: Settings,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        del timeout  # 向后兼容旧签名；超时一律取自 Settings（spec §4.2）
        self.base_url = f"http://{settings.model_server_host}:{settings.model_server_port}"
        self._model_dir = settings.model_dir.replace("\\", "/")
        self._connect_timeout_s = settings.model_connect_timeout_s
        self._prefill_timeout_s = settings.model_prefill_timeout_s
        self._stream_idle_timeout_s = settings.model_stream_idle_timeout_s
        self._round_timeout_s = settings.model_round_timeout_s
        self._retry_count = settings.model_retry_count
        self._retry_backoff_s = settings.model_retry_backoff_s
        base_timeout = httpx.Timeout(
            connect=self._connect_timeout_s,
            read=self._stream_idle_timeout_s,
            write=10.0,
            pool=10.0,
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=base_timeout, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        try:
            resp = await self._client.get("/health", timeout=self._connect_timeout_s)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            raise ModelServerError(f"模型服务不可达: {e}") from e

    # ---------- 会话生命周期（spec §2 新增） ----------

    async def init_session(self) -> dict[str, Any]:
        """① omni_init：建立会话。

        use_tts=false 是 B1 硬约束（true 会拖首 token 至 44s）。
        """
        payload = {
            "media_type": 2,
            "use_tts": False,
            "duplex_mode": False,
            "model_dir": self._model_dir,
        }
        timeout = httpx.Timeout(
            connect=self._connect_timeout_s,
            read=self._prefill_timeout_s,  # 首次冷启动可达 35s
            write=10.0,
            pool=10.0,
        )
        try:
            resp = await self._client.post("/v1/stream/omni_init", json=payload, timeout=timeout)
            resp.raise_for_status()
            session = resp.json()
        except httpx.TransportError as e:
            raise ModelNetworkError(f"omni_init 网络错误: {e}") from e
        except httpx.HTTPStatusError as e:
            raise ModelError(f"omni_init 失败: HTTP {e.response.status_code}") from e
        except ValueError as e:  # resp.json() 非 JSON
            raise SseProtocolError(f"omni_init 返回非 JSON: {e}") from e
        if not isinstance(session, dict):
            raise SseProtocolError(f"omni_init 返回非对象: {session!r}")
        return session

    async def prefill_image(self, session: dict[str, Any], image_path: Path, cnt: int = 0) -> None:
        """②-a 图片 prefill（POC-001 实测 cnt=0）"""
        del session
        await self._prefill({"img_path_prefix": str(image_path), "cnt": cnt})

    async def prefill_text(self, session: dict[str, Any], text: str, cnt: int = 1) -> None:
        """②-b 文本 prefill（POC-001 实测 cnt=1）"""
        del session
        await self._prefill({"text": text, "cnt": cnt})

    async def _prefill(self, body: dict[str, Any]) -> None:
        timeout = httpx.Timeout(
            connect=self._connect_timeout_s,
            read=self._prefill_timeout_s,
            write=10.0,
            pool=10.0,
        )
        try:
            resp = await self._client.post("/v1/stream/prefill", json=body, timeout=timeout)
            resp.raise_for_status()
        except httpx.TransportError as e:
            raise ModelNetworkError(f"prefill 网络错误: {e}") from e
        except httpx.HTTPStatusError as e:
            raise ModelError(f"prefill 失败: HTTP {e.response.status_code}") from e

    # ---------- 推理（spec §2 改造） ----------

    async def decode_stream(self, session: dict[str, Any], max_tokens: int) -> AsyncIterator[str]:
        """③ stream=true 流式解码，逐块 yield 文本增量。

        - SSE 解析用 sse.iter_sse_chunks（畸形行 → SseProtocolError）
        - 流空闲超时（read=stream_idle）→ ModelNetworkError
        - 缺 [DONE] 终止标记 → SseProtocolError
        """
        body = {
            "stream": True,
            "max_tokens": max_tokens,
            "round_idx": self._round_idx(session),
            "length_penalty": 1.1,
            "use_tts": False,
        }
        stream_timeout = httpx.Timeout(
            connect=self._connect_timeout_s,
            read=self._stream_idle_timeout_s,
            write=10.0,
            pool=10.0,
        )
        saw_done = False
        try:
            async with self._client.stream(
                "POST", "/v1/stream/decode", json=body, timeout=stream_timeout
            ) as resp:
                if resp.status_code >= 400:
                    raise ModelError(f"decode 失败: HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("application/json"):
                    raise SseProtocolError(f"decode 非 SSE 响应: {ctype}")
                async for ev in iter_sse_chunks(resp):
                    if ev.kind == "done":
                        saw_done = True
                        break
                    if ev.kind == "error":
                        raise ModelError(f"模型返回错误: {ev.content}")
                    if ev.kind == "delta" and ev.content:
                        yield ev.content
        except SseParseError as e:
            raise SseProtocolError(str(e)) from e
        except httpx.TransportError as e:
            raise ModelNetworkError(f"decode 流中断: {e}") from e
        if not saw_done:
            raise SseProtocolError("decode 流内缺 [DONE] 终止标记")

    async def vision_analyze(self, image_path: Path, prompt: str, max_tokens: int = 256) -> str:
        """单帧视觉判定：init → prefill(img) → prefill(text) → decode(SSE 拼接)。

        返回纯文本（含 <think> 块与最终 JSON），供 VisionAnalyzer.parse_vision_output。
        """
        if not image_path.exists():
            raise ModelError(f"截图不存在: {image_path}")
        return await self._run_with_retry("vision_analyze", self._run_vision, image_path, prompt, max_tokens)

    async def chat(self, prompt: str, max_tokens: int = 512) -> str:
        """纯文本对话（任务建议/问答用，V1 保留）：init → prefill(text) → decode"""
        return await self._run_with_retry("chat", self._run_chat, prompt, max_tokens)

    # ---------- 内部 ----------

    async def _run_vision(self, image_path: Path, prompt: str, max_tokens: int) -> str:
        session = await self.init_session()
        await self.prefill_image(session, image_path)
        await self.prefill_text(session, prompt)
        parts: list[str] = []
        async for chunk in self.decode_stream(session, max_tokens):
            parts.append(chunk)
        return "".join(parts)

    async def _run_chat(self, prompt: str, max_tokens: int) -> str:
        session = await self.init_session()
        await self.prefill_text(session, prompt)
        parts: list[str] = []
        async for chunk in self.decode_stream(session, max_tokens):
            parts.append(chunk)
        return "".join(parts)

    async def _run_with_retry(self, name: str, coro_fn, *args) -> str:
        """整轮（init→prefill→decode）执行；网络类错误重试 1 次；整轮 120s 上限"""
        last_error: Exception | None = None
        for attempt in range(self._retry_count + 1):
            start = time.perf_counter()
            try:
                text = await asyncio.wait_for(coro_fn(*args), timeout=self._round_timeout_s)
                elapsed = int((time.perf_counter() - start) * 1000)
                logger.info("%s ok: total=%dms chars=%d", name, elapsed, len(text))
                return text
            except asyncio.TimeoutError:
                last_error = ModelNetworkError(f"{name} 整轮超时 {self._round_timeout_s}s")
            except ModelNetworkError as e:
                last_error = e
            if attempt < self._retry_count:
                logger.warning(
                    "%s 网络错误，重试 %d/%d: %s", name, attempt + 1, self._retry_count, last_error
                )
                await asyncio.sleep(self._retry_backoff_s)
        raise ModelServerError(f"{name} 失败（重试 {self._retry_count} 次后）: {last_error}") from last_error

    @staticmethod
    def _round_idx(session: dict[str, Any]) -> int:
        raw = session.get("round_idx", 0)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
