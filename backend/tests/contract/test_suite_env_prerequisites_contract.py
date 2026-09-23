"""契约：整个 backend 测试套件必须在**干净环境**下可收集、可运行。

2026-09-24 实测事故（这条契约就是为它立的）
────────────────────────────────────────────────────────────────────────────
CI（ubuntu，干净检出）跑 `pytest backend/tests/contract`，套件在**收集阶段**整体死掉，
连一条断言都没跑到：

    app.voice.config.ProductionGateError: runtime hello security capability missing:
        private_key_pem, public_key_pem, rtc_bridge_credential,
        certificate_binding, gateway_assertion
    1 error during collection → pytest rc=2

`app.main` 在**导入期**装配生产应用（backend/app/main.py:270），hello 装配 fail-closed
（backend/app/voice/hello_runtime.py:42-45）。它此前"一直绿"，只是因为开发机仓库根有一份
**未跟踪**的 .env 提供真值 —— **一个只在某些机器上能通过的守卫不叫守卫。**

同一根因还有**运行期**的一面（收集成功也照样踩）：缺 `VOICE_OWNER_CREDENTIAL` /
`VOICE_SIDECAR_CREDENTIAL` 时，安全路由返回的 503（能力缺失）会被误判成"401 被改坏了"。
所以本文件除了收集，还要在干净环境下真跑那条 CORS 鉴权契约。

本文件怎么测
────────────────────────────────────────────────────────────────────────────
在子进程里把这些键显式置为**空串**，再收集/运行。

为什么用空串而不是 unset：unset 会被开发机的 .env 兜住，那样这条测试在本地就是假绿。
空串是**显式提供的值**，优先级高于 .env（backend/app/config.py:162 的 env_file），
因此空串 ≈ "确实没人提供"，与 CI 的干净环境等价，且在**任何机器上都一样严格**。

后置真对照（test_the_gate_is_not_vacuous_without_pytest）证明：同样的空串环境下，
绕开 pytest/conftest 裸导入 app.main **必须失败**。一正一反，才排除了
"门禁本来就不过敏"这种假绿。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFTEST = ROOT / "backend" / "tests" / "conftest.py"
CORS_CONTRACT = "backend/tests/contract/test_sidecar_renderer_cors_contract.py"

# 与 backend/app/main.py:144-152 传给 build_hello_runtime 的 5 个入参一一对应。
# 刻意在这里重抄一份而不 import conftest：被检查对象是 conftest 本身。
HELLO_CAPABILITY_ENV = (
    "VOICE_HELLO_PRIVATE_KEY_PEM",
    "VOICE_HELLO_PUBLIC_KEY_PEM",
    "VOICE_RTC_BRIDGE_CREDENTIAL",
    "VOICE_RTC_BRIDGE_CERT_BINDING",
    "VOICE_GATEWAY_SHARED_ASSERTION",
)

# 控制面鉴权凭据：缺了它们安全路由会返回 503（能力缺失）而不是 401（未授权）。
CONTROL_PLANE_CREDENTIAL_ENV = (
    "VOICE_OWNER_CREDENTIAL",
    "VOICE_SIDECAR_CREDENTIAL",
)

ALL_DECLARED_ENV = HELLO_CAPABILITY_ENV + CONTROL_PLANE_CREDENTIAL_ENV

# 三处都会走到 app.main 的导入期装配，因此都要在干净环境下可收集。
SUITES = ("backend/tests/contract", "backend/tests/integration", "backend/tests/unit")


def _clean_env() -> dict[str, str]:
    """抹掉全部 VOICE_*，再把 conftest 应当声明的键置为空串（空串覆盖 .env）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICE_")}
    for name in ALL_DECLARED_ENV:
        env[name] = ""
    return env


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _pytest(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, env=_clean_env(), capture_output=True, text=True,
    )


def test_conftest_exists_to_declare_the_suites_environment_prerequisites() -> None:
    assert CONFTEST.is_file(), (
        f"{CONFTEST} 不存在。它是 app.main 导入期装配所必需的环境前提的唯一声明处；"
        "删掉它会让整层契约在干净检出上收集失败。"
    )


def test_backend_suites_collect_with_no_configured_capabilities() -> None:
    """干净环境下必须能收集 —— 否则门禁会死在"还没跑到断言"的地方。"""
    result = _pytest([*SUITES, "--collect-only"])
    assert result.returncode == 0, (
        "干净环境下无法收集 backend 测试套件 —— 说明有测试隐含依赖开发机的 .env，"
        "或 backend/tests/conftest.py 被删/漏项。\n"
        f"--- stdout tail ---\n{_tail(result.stdout)}\n"
        f"--- stderr tail ---\n{_tail(result.stderr)}"
    )


def test_cors_auth_contract_holds_with_no_configured_capabilities() -> None:
    """运行期那一面：干净环境下必须 401（守卫在前），不能退化成 503（能力缺失）。

    只测收集是抓不到这一条的 —— 收集成功之后应用照样可以在运行期返回 503。
    """
    result = _pytest([CORS_CONTRACT])
    assert result.returncode == 0, (
        "干净环境下 CORS 鉴权契约未通过 —— 多半是 conftest 没补 "
        f"{CONTROL_PLANE_CREDENTIAL_ENV}，于是安全路由返回 503 而非 401。\n"
        f"--- stdout tail ---\n{_tail(result.stdout)}\n"
        f"--- stderr tail ---\n{_tail(result.stderr)}"
    )


def test_the_gate_is_not_vacuous_without_pytest() -> None:
    """后置真对照：绕开 pytest/conftest 裸导入 app.main，必须因缺能力而失败。

    没有这一条，上面那条收集测试可能是"门禁本来就不过敏"造成的假绿 ——
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
