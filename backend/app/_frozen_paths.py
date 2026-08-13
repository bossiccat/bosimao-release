"""冻结态（PyInstaller）路径解析助手。

背景：源码态下多处模块用 ``Path(__file__).resolve().parents[N]`` 推导项目根目录。
PyInstaller 打包后 ``__file__`` 指向解压临时目录 ``_MEIxxxx``，会导致 ``config/``、``.env``、
``backend/data/``、``models/`` 等外部资源全部解析错位。

约定（打包部署布局）：
- ``jax-backend.exe`` 部署在「项目根」目录（与 ``config/``、``.env``、``backend/``、``models/`` 同级）。
- 运行时可变资源（config、.env、data、models）留在磁盘上，绝不打进 exe。
- 只读代码数据（如 ``app/voice/migrations/*.sql``）通过 :func:`bundled_path` 打进 exe 并从 ``_MEIPASS`` 读取。

本模块不得导入任何业务模块，避免循环依赖。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """PyInstaller 冻结态判定（bootloader 会注入 sys.frozen）。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """项目根目录：冻结态 = exe 所在目录；源码态 = backend/app 上溯两级。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled_path(*parts: str) -> Path:
    """只读代码数据的落点：冻结态取 ``_MEIPASS``（解压目录），源码态取项目根。"""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = project_root()
    return base.joinpath(*parts)
