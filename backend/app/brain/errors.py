"""大脑业务错误（统一聚合出口，路由层映射为 HTTP + spec 错误码）

错误码见 backend-brain-spec §9.2：
- 40001 意图为空/超长/非法 source
- 40201 DeepSeek 不可用（熔断/网络/认证失败）
- 40301 task_id 不存在
- 40302 任务状态不允许该操作 / 确认令牌无效
- 40303 注入前置失败（目标窗口未聚焦/标题不匹配）
- 40901 注入进行中（幂等）
- 42901 重生成超频
- 50301 服务未就绪（本地 9B 不可用 / DEEPSEEK_API_KEY 未配置）
"""
from __future__ import annotations


class BrainError(Exception):
    """大脑业务错误：携带 spec 错误码 + HTTP 状态码 + 用户可读信息"""

    def __init__(self, code: int, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# ---- 便捷构造（按 spec §9.2） ----
def intent_invalid(message: str = "意图为空/超长/非法") -> BrainError:
    return BrainError(40001, 400, message)


def deepseek_unavailable(message: str = "DeepSeek 不可用") -> BrainError:
    return BrainError(40201, 402, message)


def task_not_found(message: str = "task_id 不存在") -> BrainError:
    return BrainError(40301, 403, message)


def status_not_allowed(message: str = "任务状态不允许该操作") -> BrainError:
    return BrainError(40302, 403, message)


def focus_failed(message: str = "目标窗口未聚焦") -> BrainError:
    return BrainError(40303, 403, message)


def inject_in_progress(message: str = "注入进行中") -> BrainError:
    return BrainError(40901, 409, message)


def regenerate_limited(message: str = "重生成超频，请 1 分钟后再试") -> BrainError:
    return BrainError(42901, 429, message)


def service_unready(message: str = "服务未就绪") -> BrainError:
    return BrainError(50301, 503, message)
