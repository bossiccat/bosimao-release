"""DeepSeek V4 Flash 客户端（OpenAI 兼容，backend-brain-spec §3）

- 非流式 JSON（stream=false，规避 SSE 坑，spec §11.3）
- 显式 trust_env=False（防 127.0.0.1:7890 残留代理误判，spec §11.1）
- 错误四分类 + 重试：
  网络类（TransportError/超时）重试 2 次（退避 1s、2s）
  429 / 5xx 重试 1 次（退避 3s）
  401/403 不重试 → DeepSeekAuthError（提示检查 DEEPSEEK_API_KEY）
  协议错（非 JSON / usage 缺失）不重试
- 熔断：连续失败 ≥3 → 熔断 300s；期间 route() 降级本地
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from ..config import BrainConfig, DeepSeekConfig, Settings

logger = logging.getLogger(__name__)


class DeepSeekError(Exception):
    """DeepSeek 错误聚合基类"""


class DeepSeekNetworkError(DeepSeekError):
    """网络类错误（连接/传输层）——可重试"""


class DeepSeekTimeoutError(DeepSeekNetworkError):
    """超时——可重试"""


class DeepSeekHttpError(DeepSeekError):
    """HTTP 错误（4xx/5xx）"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class DeepSeekAuthError(DeepSeekHttpError):
    """401/403——不重试，报配置错误"""


class DeepSeekRateLimitError(DeepSeekHttpError):
    """429——退避重试"""


class DeepSeekProtocolError(DeepSeekError):
    """响应非 JSON / usage 缺失——不重试"""


class DeepSeekClient:
    """DeepSeek 客户端（OpenAI 兼容 POST {base_url}/chat/completions）"""

    def __init__(
        self,
        settings: Settings,
        cfg: BrainConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        d: DeepSeekConfig = cfg.deepseek
        self._base_url = (settings.deepseek_base_url or "https://api.deepseek.com/v1").rstrip("/")
        self._api_key = settings.deepseek_api_key or ""
        self._model = settings.deepseek_model or "deepseek-v4-flash"
        self._network_retries = d.network_retries
        self._network_backoff = list(d.network_backoff_s)
        self._http_retries = d.http_retries
        self._http_backoff = d.http_backoff_s
        self._fail_threshold = d.circuit_fail_threshold
        self._cooldown_s = d.circuit_cooldown_s
        self._connect_timeout = d.connect_timeout_s
        self._read_timeout = d.read_timeout_s
        self._total_timeout = d.total_timeout_s

        timeout = httpx.Timeout(
            connect=self._connect_timeout, read=self._read_timeout, write=30.0, pool=30.0
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            trust_env=False,  # 防 7890 残留代理误判（spec §11.1）
            transport=transport,
        )
        self._fail_count = 0
        self._circuit_until = 0.0

    # ---------- 状态 ----------
    def key_configured(self) -> bool:
        return bool(self._api_key)

    def circuit_open(self) -> bool:
        return time.time() < self._circuit_until

    def circuit_state(self) -> dict[str, Any]:
        return {
            "open": self.circuit_open(),
            "fail_count": self._fail_count,
            "cooldown_until": self._circuit_until,
        }

    def _record_success(self) -> None:
        self._fail_count = 0
        self._circuit_until = 0.0

    def _record_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= self._fail_threshold:
            self._circuit_until = time.time() + self._cooldown_s
            logger.warning(
                "DeepSeek 熔断 %ds（连续失败 %d 次）", self._cooldown_s, self._fail_count
            )

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- 主调用 ----------
    async def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        data = await self._request(payload)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise DeepSeekProtocolError(f"DeepSeek 响应缺 choices[0].message.content: {e}") from e

    async def chat_json(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        json_schema: dict | None = None,
        temperature: float = 0.2,
    ) -> dict:
        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        del json_schema  # V4 Flash 走 response_format=json_object；schema 仅作 prompt 约束
        data = await self._request(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise DeepSeekProtocolError(f"DeepSeek 响应缺 choices[0].message.content: {e}") from e
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, TypeError) as e:
            raise DeepSeekProtocolError(f"DeepSeek 返回非 JSON 内容: {e}") from e
        if not isinstance(obj, dict):
            raise DeepSeekProtocolError(f"DeepSeek JSON 非对象: {obj!r}")
        return obj

    async def health(self) -> bool:
        """轻量探测（供 /health 与路由判断）；不记入熔断计数"""
        if not self.key_configured():
            return False
        try:
            await self.chat([{"role": "system", "content": "ping"}], max_tokens=8)
            return True
        except DeepSeekError:
            return False

    # ---------- 重试 / 熔断编排 ----------
    async def _request(self, payload: dict) -> dict:
        if self.circuit_open():
            raise DeepSeekNetworkError("DeepSeek 熔断中（冷却期内）")
        net_done = 0
        http_done = 0
        last_err: DeepSeekError | None = None
        while True:
            try:
                data = await self._post(payload)
                self._record_success()
                return data
            except DeepSeekAuthError as e:
                logger.error("DeepSeek 认证失败（检查 DEEPSEEK_API_KEY）: %s", e)
                self._record_failure()
                raise
            except DeepSeekProtocolError as e:
                logger.warning("DeepSeek 协议错误（不重试）: %s", e)
                self._record_failure()
                raise
            except (DeepSeekNetworkError, DeepSeekTimeoutError) as e:
                last_err = e
                if net_done < self._network_retries:
                    backoff = self._network_backoff[net_done] if net_done < len(self._network_backoff) else 1.0
                    logger.warning("DeepSeek 网络错误，重试 %d/%d: %s", net_done + 1, self._network_retries, e)
                    await asyncio.sleep(backoff)
                    net_done += 1
                    continue
                self._record_failure()
                raise
            except DeepSeekHttpError as e:
                retryable = isinstance(e, DeepSeekRateLimitError) or e.status >= 500
                last_err = e
                if retryable and http_done < self._http_retries:
                    logger.warning("DeepSeek HTTP %s，退避重试: %s", e.status, e)
                    await asyncio.sleep(self._http_backoff)
                    http_done += 1
                    continue
                self._record_failure()
                raise
        raise DeepSeekNetworkError(f"DeepSeek 请求失败: {last_err}")  # pragma: no cover

    async def _post(self, payload: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = await self._client.post("/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise DeepSeekTimeoutError(f"DeepSeek 请求超时: {e}") from e
        except httpx.TransportError as e:
            raise DeepSeekNetworkError(f"DeepSeek 网络错误: {e}") from e

        status = resp.status_code
        if status in (401, 403):
            raise DeepSeekAuthError(f"DeepSeek 认证失败 HTTP {status}", status=status)
        if status == 429:
            raise DeepSeekRateLimitError("DeepSeek 限流 HTTP 429", status=status)
        if status >= 500:
            raise DeepSeekHttpError(f"DeepSeek 服务端错误 HTTP {status}", status=status)
        if status >= 400:
            raise DeepSeekHttpError(f"DeepSeek 请求错误 HTTP {status}", status=status)

        try:
            data = resp.json()
        except ValueError as e:
            raise DeepSeekProtocolError(f"DeepSeek 响应非 JSON: {e}") from e
        if not isinstance(data, dict) or "usage" not in data:
            raise DeepSeekProtocolError("DeepSeek 响应缺 usage 字段")
        return data
