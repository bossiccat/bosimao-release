"""Worker profile allowlist —— 审批绑定与命令组装的单一事实源。

任何新 profile 必须先过安全审查再入表；argv 中禁止出现凭据名、用户文本或副作用工具。
"""
from __future__ import annotations

# 服务端锁定的只读 profile allowlist。
WORKER_PROFILES: dict[str, tuple[str, ...]] = {
    # 默认安全模式：纯 CLI 帮助，零网络、零副作用。
    "probe_help": ("--help",),
    # 受控只读 DeepSeek 推理：显式 model+provider 路由（隐式 provider 推断已证实
    # 不可靠，会命中 HTTP 401），空 toolsets 禁用一切工具副作用，oneshot 单轮、
    # 提示词固定为 ping。
    "deepseek_readonly": (
        "-z",
        "--model", "deepseek-v4-flash-0731",
        "--provider", "kkdmx",
        "--toolsets", "",
        "ping",
    ),
}

DEFAULT_PROFILE = "probe_help"

# 启动校验必需的绑定键；缺任一键视为旧格式/篡改命令，拒绝启动。
REQUIRED_BINDING_KEYS = ("profile", "command_argv")


def worker_command_argv(profile: str) -> tuple[str, ...]:
    """返回 profile 锁定的参数序列（不含二进制）。未知 profile 抛 KeyError。"""
    return WORKER_PROFILES[profile]


def binding_matches(profile: str, command_argv: tuple[str, ...]) -> bool:
    """校验审批绑定的摘要与当前 allowlist 是否逐字节一致（摘要含规范二进制名前缀）。"""
    if profile not in WORKER_PROFILES:
        return False
    expected = ("hermes", *WORKER_PROFILES[profile])
    return tuple(command_argv) == expected
