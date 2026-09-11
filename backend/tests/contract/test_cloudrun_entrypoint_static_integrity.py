"""CloudRun 入口文件的静态完整性契约。

背景（真实生产事故）：`cloudapi/main.py` 加了 `@asynccontextmanager` 却没写
`from contextlib import asynccontextmanager`。本地 1026 个单测全绿——因为没有任何
测试真正编译/加载这个文件（它只在容器里被 uvicorn import），结果容器启动即崩：

    /srv/main.py line 129  @asynccontextmanager
    NameError: name 'asynccontextmanager' is not defined

因此这里用 symtable 做**模块级未定义名**检查：容器入口不需要装依赖、不需要起服务，
就能在 CI 里拦住这类"本地测不到"的错误。
"""
from __future__ import annotations

import builtins
import py_compile
import symtable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINTS = (
    ROOT / "cloudapi" / "main.py",
    ROOT / "backend" / "app" / "main.py",
)

# 解释器注入的模块级名字，不属于"未定义"。
_INJECTED = {
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__annotations__", "__future__", "__dict__",
}
_KNOWN = set(dir(builtins)) | _INJECTED


def _undefined_module_names(source: str, filename: str) -> list[str]:
    """返回模块顶层被引用但从未被赋值/导入/定义的名字。

    只看顶层符号表（module scope）：函数内部的名字落在子表里，由解释器在调用时
    才解析，不属于本检查范围——而事故恰恰发生在模块顶层。
    """
    table = symtable.symtable(source, filename, "exec")
    undefined = []
    for symbol in table.get_symbols():
        name = symbol.get_name()
        if not symbol.is_referenced():
            continue
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace():
            continue
        if name in _KNOWN:
            continue
        undefined.append(name)
    return undefined


def test_cloudrun_entrypoints_compile() -> None:
    """入口文件必须能编译（语法错误在容器里等于直接起不来）。"""
    for path in ENTRYPOINTS:
        py_compile.compile(str(path), cfile=None, doraise=True)


def test_cloudrun_entrypoints_have_no_undefined_module_level_names() -> None:
    """模块顶层不得出现未导入/未定义的名字。

    这条会拦住 `@asynccontextmanager` 忘记 import 这类 bug——它只在容器启动时
    才以 NameError 暴露，本地单测覆盖不到。
    """
    problems = {}
    for path in ENTRYPOINTS:
        source = path.read_text(encoding="utf-8")
        undefined = _undefined_module_names(source, str(path))
        if undefined:
            problems[str(path.relative_to(ROOT))] = undefined
    assert not problems, f"入口文件存在模块级未定义名: {problems}"


def test_detector_flags_the_real_asynccontextmanager_bug() -> None:
    """自检：检测器必须能复现并抓住当初那个真实事故。

    本用例与生产代码无关，只验证检查逻辑本身有效——否则上面那条测试可能是
    "因为检测器永远不报错"而假绿。
    """
    broken = (
        "from fastapi import FastAPI\n"
        "\n"
        "@asynccontextmanager\n"
        "async def lifespan(app):\n"
        "    yield\n"
    )
    assert "asynccontextmanager" in _undefined_module_names(broken, "broken.py")

    fixed = (
        "from contextlib import asynccontextmanager\n"
        "from fastapi import FastAPI\n"
        "\n"
        "@asynccontextmanager\n"
        "async def lifespan(app):\n"
        "    yield\n"
    )
    assert _undefined_module_names(fixed, "fixed.py") == []
