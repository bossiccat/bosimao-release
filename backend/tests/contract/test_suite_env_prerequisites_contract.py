"""契约：整个 backend 测试套件必须在**干净环境**下可收集。

2026-09-24 实测事故（这条契约就是为它立的）
────────────────────────────────────────────────────────────────────────────
CI（ubuntu，干净检出）执行 `pytest backend/tests/contract`，套件在**收集阶段**整体死掉，
连一条断言都没跑到：

    app.voice.config.ProductionGateError: runtime hello security capability missing:
        private_key_pem, public_key_pem, rtc_bridge_credential,
        certificate_binding, gateway_assertion
    1 error during collection → pytest rc=2

`app.main` 在**导入期**装配生产应用（backend/app/main.py:270），hello 装配 fail-closed
（backend/app/voice/hello_runtime.py:42-45：5 项任一为空即抛）。它此前"一直绿"，
只是因为开发机仓库根有一份**未跟踪**的 .env 提供了真值 ——
**一个只在某些机器上能通过的守卫不叫守卫。**

本文件怎么测
────────────────────────────────────────────────────────────────────────────
在子进程里把 5 项能力显式置为**空串**，然后只做 `--collect-only`。

为什么用空串而不是 unset：unset 会被开发机的 .env 兜住，那样这条测试在本地就是假绿。
空串是**显式提供的值**，优先级高于 .env（backend/app/config.py:162 的 env_file），
因此空串 ≈ "确实没人提供能力"，与 CI 的干净环境等价，且在**任何机器上都一样严格**。

配套的后置真对照（test_the_gate_is_not_vacuous_without_pytest）证明：
同样的空串环境下，绕开 pytest/conftest 裸导入 app.main **必须失败**。
两条一正一反，才排除了"门禁本来就不过敏"这种假绿。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFTEST = ROOT / "backend" / "tests" / "conftest.py"

# 与 backend/app/main.py:144-152 传给 build_hello_runtime 的 5 个入参一一对应。
# 刻意在这里重抄一份而不 import conftest：被检查对象是 conftest 本身。
CAPABILITY_ENV = (
    "VOICE_HELLO_PRIVATE_KEY_PEM",
    "VOICE_HELLO_PUBLIC_KEY_PEM",
    "VOICE_RTC_BRIDGE_CREDENTIAL",
    "VOICE_RTC_BRIDGE_CERT_BINDING",
    "VOICE_GATEWAY_SHARED_ASSERTION",
)

# 三处都会走到 app.main 的导入期装配，因此都要在干净环境下可收集。
SUITES = ("backend/tests/contract", "backend/tests/integration", "backend/tests/unit")


def _clean_env() -> dict[str, str]:
    """抹掉全部 VOICE_*，再把 5 项能力置为空串（空串覆盖 .env）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICE_")}
    for name in CAPABILITY_ENV:
        env[name] = ""
    return env


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def test_conftest_exists_to_declare_the_suites_environment_prerequisites() -> None:
    assert CONFTEST.is_file(), (
        f"{CONFTEST} 不存在。它是 app.main 导入期装配所必需的环境前提的唯一声明处；"
        "删掉它会让整层契约在干净检出上收集失败。"
    )


def test_backend_suites_collect_with_no_hello_capabilities() -> None:
    """干净环境下必须能收集 —— 否则门禁会死在"还没跑到断言"的地方。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *SUITES, "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=_clean_env(), capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "干净环境下无法收集 backend 测试套件 —— 说明有测试隐含依赖开发机的 .env，"
        "或 backend/tests/conftest.py 被删/漏项。\n"
        f"--- stdout tail ---\n{_tail(result.stdout)}\n"
        f"--- stderr tail ---\n{_tail(result.stderr)}"
    )


def test_the_gate_is_not_vacuous_without_pytest() -> None:
    """后置真对照：绕开 pytest/conftest 裸导入 app.main，必须因缺能力而失败。

    没有这一条，上面那条测试可能是"门禁本来就不过敏"造成的假绿 ——
    两条一正一反才构成完整测量。
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'backend'); from app.main import app"],
        cwd=ROOT, env=_clean_env(), capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        "空串环境下裸导入 app.main 竟然成功：不是 hello 门禁失灵，"
        "就是环境被 .env 兜住了（空串本应覆盖 .env）。两种都要先查清。"
    )
    assert "hello security capability missing" in result.stderr, (
        "导入确实失败了，但不是因为 hello 能力缺失，而是别的原因：\n"
        f"{_tail(result.stderr)}"
    )
