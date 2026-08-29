"""鉴权前置路由：让守卫在 request body 校验之前执行。

背景（2026-08-29 修）：商业语音端点原先把守卫写在端点函数体内，而 FastAPI
固定先解析并校验 request body 再进入函数体——实测 fastapi 0.141.1 下，无论
`Depends` 声明在签名何处（body 之前/之后/带 Request 注解），body 校验都优先
执行（三种写法均返回 422）。结果是未认证请求会先拿到 422 + 字段级错误明细，
泄露 request body 的 schema 结构，且违反「401 优先」契约。

方案对比：
- 依赖式守卫（Depends）：实测无效，body 仍先校验。
- 端点内收 `object` + 手动 validate（hello-redeem 模式）：可行但会丢失
  OpenAPI requestBody schema，且每个端点都要重复样板代码。
- **自定义 APIRoute（本模块）**：在路由 handler 之外先跑守卫，因此早于
  solve_dependencies（含 body 校验）；端点保留类型注解，OpenAPI schema 完整。

守卫为空（内部直连/单测）时行为不变：非法 body 仍返回 422。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.types import DecoratedCallable

GUARD_OP_ATTR = "_voice_guard_op"

DenyResponse = Any
GuardFn = Callable[[Request, str], DenyResponse | None]


def guarded(op: str) -> Callable[[DecoratedCallable], DecoratedCallable]:
    """标记端点需要守卫，并声明操作名（nonce/限流按 op 分桶）。"""
    def decorator(fn: DecoratedCallable) -> DecoratedCallable:
        setattr(fn, GUARD_OP_ATTR, op)
        return fn
    return decorator


class GuardedAPIRoute(APIRoute):
    """在 solve_dependencies（含 body 校验）之前执行守卫的 APIRoute。"""

    def __init__(self, path: str, endpoint: Callable[..., Any], *,
                 guard: GuardFn | None = None, **kwargs: Any) -> None:
        super().__init__(path, endpoint, **kwargs)
        self._guard = guard

    def get_route_handler(
        self,
    ) -> Callable[[Request], Awaitable[Any]]:
        original_handler = super().get_route_handler()

        async def guarded_handler(request: Request) -> Any:
            if self._guard is not None:
                denied = self._guard(
                    request, getattr(self.endpoint, GUARD_OP_ATTR, "")
                )
                if denied is not None:
                    return denied
            return await original_handler(request)

        return guarded_handler
