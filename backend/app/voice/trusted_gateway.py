"""Trusted reverse-proxy boundary for internal rtc_bridge identity."""
from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import CredentialValidator

logger = logging.getLogger(__name__)

_TRUSTED_SCOPE_KEY = "voice_trusted_gateway"
_STRIPPED_HEADERS = {
    b"x-client-certificate-thumbprint",
    b"x-client-certificate-verified",
}


class TrustedGatewayIdentityMiddleware:
    """Convert a loopback gateway assertion into server-owned request identity.

    Client certificate headers are always removed. Only a request arriving from an
    explicitly trusted source and presenting the reverse-proxy shared assertion can
    receive the server-side scope marker consumed by the hello redemption route.

    2026-09-08 云端部署（CloudBase CloudRun）：受信反向代理不再限于 loopback——
    平台边缘网关的来源经 VOICE_TRUSTED_GATEWAY_HOSTS 显式声明（逗号分隔，支持
    单 IP 与 CIDR，如 "127.0.0.1,::1,10.15.254.0/24"；云端边缘 LB 是 IP 池，
    实测 10.15.254.247/166 等，单 IP 白名单不可行）。默认仍为 loopback 保持
    本地语义不变。断言被拒时记录来源 IP，便于云端首次接入时发现边缘网关地址
    （fail-closed 语义不变：来源 + 断言 HMAC 双条件缺一不可）。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        gateway_assertion_hash: str,
        certificate_binding: str,
        allowed_hosts: Iterable[str] = ("127.0.0.1", "::1"),
    ) -> None:
        self.app = app
        self.gateway_assertion_hash = gateway_assertion_hash
        self.certificate_binding = certificate_binding
        self._exact_hosts: frozenset[str] = frozenset(
            host.strip() for host in allowed_hosts if host.strip()
        )
        self._networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
            net for net in self._parse_networks(allowed_hosts)
        )
        # `*` = 显式声明「不按来源 IP 收窄」（2026-09-12）。
        # 为什么需要它：媒体面回到用户机器后，每个用户的出口 IP 都不同，来源白名单
        # 在原理上无法成立（且公网网关不消毒 XFF，来源本身可伪造）。此时真正的边界
        # 只剩共享密钥（网关断言 + 服务凭证）与 TLS pinning。
        # 用 `*` 让这件事**显式且可审计** —— 好过填一个假网段来自我安慰。
        self._allow_any_source = "*" in self._exact_hosts
        if self._allow_any_source:
            logger.warning(
                "trusted gateway: source-IP narrowing is DISABLED ('*'); "
                "boundary relies on shared secrets + TLS pinning only"
            )

    @staticmethod
    def _parse_networks(spec: Iterable[str]):
        for item in spec:
            item = item.strip()
            if "/" not in item:
                continue
            try:
                yield ipaddress.ip_network(item, strict=False)
            except ValueError:
                logger.warning("invalid trusted gateway CIDR ignored: %s", item)

    def _source_trusted(self, source_host: str) -> bool:
        if self._allow_any_source:
            return True
        if source_host in self._exact_hosts:
            return True
        if not self._networks or not source_host:
            return False
        try:
            addr = ipaddress.ip_address(source_host)
        except ValueError:
            return False
        return any(addr in network for network in self._networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers", []))
        assertion = next(
            (value.decode("utf-8") for key, value in headers
             if key.lower() == b"x-internal-gateway-assertion"),
            "",
        )
        scope["headers"] = [
            (key, value) for key, value in headers
            if key.lower() not in _STRIPPED_HEADERS
        ]
        client = scope.get("client")
        source_host = client[0] if client else ""
        assertion_hash = CredentialValidator.hash_credential(assertion)
        trusted = (
            self._source_trusted(source_host)
            and bool(self.gateway_assertion_hash)
            and bool(self.certificate_binding)
            and hmac.compare_digest(assertion_hash, self.gateway_assertion_hash)
        )
        if not trusted and assertion:
            # 可观测性（fail-closed 不变）：带断言却被拒的请求记录来源 IP——
            # 云端首次接入时据此发现边缘网关网段并配置 VOICE_TRUSTED_GATEWAY_HOSTS。
            logger.warning(
                "gateway assertion rejected: source_host=%s not in trusted_hosts "
                "(configure VOICE_TRUSTED_GATEWAY_HOSTS for cloud edge)",
                source_host,
            )
        if trusted:
            scope.setdefault("state", {})[_TRUSTED_SCOPE_KEY] = {
                "certificate_binding": self.certificate_binding,
            }
        await self.app(scope, receive, send)


def trusted_certificate_binding(scope: Scope) -> str | None:
    identity = scope.get("state", {}).get(_TRUSTED_SCOPE_KEY)
    if not isinstance(identity, dict):
        return None
    binding = identity.get("certificate_binding")
    return binding if isinstance(binding, str) and binding else None
