"""推送管理器：Provider 路由 + 重试 + 熔断（半开）+ 限频

SPEC §4.1 契约：
- 路由：按 cfg.providers 顺序，第一个成功即返回
- 重试：每 Provider 对网络类错误重试 1 次（业务错误不重试）
- 熔断：连续失败 >= fail_threshold 打开熔断；冷却到期后首个请求进入半开（probe），
  成功 → fail_count 归零关闭；失败 → 重新熔断
- 限频：由各 Provider 自行实现（Wecom 仅成功后占配额）
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import PushConfig, Settings
from .base import PushResult, PushService
from .ntfy import NtfyProvider
from .wecom import WecomProvider

logger = logging.getLogger(__name__)


class PushManager:
    """按 providers 顺序路由；熔断后切换下一 Provider"""

    def __init__(self, cfg: PushConfig, settings: Settings) -> None:
        self._cfg = cfg
        self._providers: dict[str, PushService] = {}
        self._fail_count: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

        if cfg.wecom.enabled:
            try:
                self._providers["wecom"] = WecomProvider(
                    settings.wecom_webhook_url, cfg.wecom.rate_limit_per_minute
                )
            except ValueError as e:
                logger.warning("wecom provider 未启用: %s", e)
        if cfg.ntfy.enabled:
            try:
                self._providers["ntfy"] = NtfyProvider(
                    settings.ntfy_server, settings.ntfy_topic, cfg.ntfy.priority
                )
            except ValueError as e:
                logger.warning("ntfy provider 未启用: %s", e)

    def available(self) -> list[str]:
        return list(self._providers)

    # ---------- 熔断状态 ----------
    def _circuit_open(self, name: str) -> bool:
        """熔断打开：cooldown 期间拒绝一切请求"""
        if name not in self._cooldown_until:
            return False
        return time.time() < self._cooldown_until[name]

    def _in_half_open(self, name: str) -> bool:
        """半开：冷却已到期但 fail_count 仍达阈值 → 放行一个 probe 请求"""
        return (
            self._fail_count.get(name, 0) >= self._cfg.circuit_breaker.fail_threshold
            and not self._circuit_open(name)
        )

    def _open_circuit(self, name: str) -> None:
        self._cooldown_until[name] = (
            time.time() + self._cfg.circuit_breaker.cooldown_seconds
        )
        logger.warning(
            "provider %s 熔断 %ds", name, self._cfg.circuit_breaker.cooldown_seconds
        )

    def _record_failure(self, name: str) -> None:
        self._fail_count[name] = self._fail_count.get(name, 0) + 1
        if self._fail_count[name] >= self._cfg.circuit_breaker.fail_threshold:
            self._open_circuit(name)

    def _record_success(self, name: str) -> None:
        self._fail_count[name] = 0
        self._cooldown_until.pop(name, None)

    # ---------- 调用（异常包住 + 网络类错误重试 1 次） ----------
    def _call(
        self,
        provider: PushService,
        text: str,
        image: Path | None,
        title: str | None,
    ) -> tuple[bool, PushResult]:
        """调用 provider.push：异常视为失败记录；网络类错误重试 1 次"""
        try:
            result = provider.push(text, image=image, title=title)
        except Exception as e:  # noqa: BLE001
            logger.exception("provider %s push 抛异常: %s", provider.name, e)
            return False, PushResult(False, provider.name, f"异常: {e}")
        if result.ok:
            return True, result
        if result.retryable:
            logger.info("provider %s 网络类错误，重试 1 次: %s", provider.name, result.error)
            try:
                result = provider.push(text, image=image, title=title)
            except Exception as e:  # noqa: BLE001
                logger.exception("provider %s 重试抛异常: %s", provider.name, e)
                return False, PushResult(False, provider.name, f"重试异常: {e}")
            if result.ok:
                return True, result
        return False, result

    # ---------- 主入口 ----------
    def push(
        self,
        text: str,
        image: Path | None = None,
        title: str | None = None,
    ) -> PushResult:
        """按配置顺序尝试各 Provider；第一个成功即返回"""
        for name in self._cfg.providers:
            provider = self._providers.get(name)
            if provider is None or self._circuit_open(name):
                continue
            if self._in_half_open(name):
                # 半开 probe：成功关闭熔断，失败重新熔断
                ok, result = self._call(provider, text, image, title)
                if ok:
                    self._record_success(name)
                    logger.info("push ok (half-open recovered): provider=%s", name)
                    return result
                self._record_failure(name)
                logger.warning("push probe failed: provider=%s err=%s", name, result.error)
                continue
            ok, result = self._call(provider, text, image, title)
            if ok:
                self._record_success(name)
                logger.info("push ok: provider=%s", name)
                return result
            self._record_failure(name)
            logger.warning("push failed: provider=%s err=%s", name, result.error)

        return PushResult(False, "none", "所有 Provider 均失败")
