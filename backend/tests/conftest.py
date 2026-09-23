"""backend 测试套件的**环境前提**，统一在此声明。

为什么需要这个文件（2026-09-24 实测事故）
────────────────────────────────────────────────────────────────────────────
CI（ubuntu，干净检出）执行 `pytest backend/tests/contract` 时，套件在**收集阶段**
就整体死掉，连一条断言都没跑到：

    app.voice.config.ProductionGateError: runtime hello security capability missing:
        private_key_pem, public_key_pem, rtc_bridge_credential,
        certificate_binding, gateway_assertion
    1 error during collection → pytest rc=2

成因：`app.main` 在**导入期**就装配生产应用（backend/app/main.py:270），而 hello
装配是 fail-closed 的（backend/app/voice/hello_runtime.py:42-45：5 项任一为空即抛）。
`backend/tests/contract/test_sidecar_renderer_cors_contract.py` 与
`backend/tests/unit/test_health_proc_signature.py` 都会走到它。

为什么以前"一直绿"：开发机仓库根目录有一份**未跟踪**的 `.env`
（backend/app/config.py:162 用**绝对路径** `str(PROJECT_ROOT / ".env")` 读取），
它提供了这 5 项的真值。于是这个守卫只在"有那份本地文件"的机器上成立 ——
**一个只在某些机器上能通过的守卫不叫守卫。**

本文件做什么
────────────────────────────────────────────────────────────────────────────
仅当环境里没有**非空值**时，才补上测试值。三方由此都正确：
  · 部署流水线（deploy-cloudrun.yml 作业级 env 从 Secrets 注入真值）→ 真值生效；
  · 干净检出（CI / 新同事的机器）→ 补测试值，套件可收集、可运行；
  · 开发机（只有 .env）→ 也用测试值，于是每台机器结果一致（确定性）。

⚠️ 用 `not os.environ.get(...)` 而**不是** `os.environ.setdefault(...)`：
`setdefault` 会把**空串**当成"已设置"而放过，而空串与未设置对 fail-closed 门禁是
等价的 —— 那正是本次事故的形态。

测试值只需**非空**：hello_runtime 在装配期不解析密钥材料（只判非空）。
⚠️ 将来若有测试真的要签名或验签，请在那里显式注入真实的测试密钥，
不要假定本文件给的是可用密钥。

配套守卫：`backend/tests/contract/test_suite_env_prerequisites_contract.py`
会在子进程里把这 5 项置为空串（空串会覆盖 .env，因此开发机上也一样严格）后做
`--collect-only`，一旦本文件被删或漏项，那条守卫立刻变红。
"""
from __future__ import annotations

import os

# 与 backend/app/main.py:144-152 传给 build_hello_runtime 的 5 个入参一一对应。
HELLO_CAPABILITY_ENV = (
    "VOICE_HELLO_PRIVATE_KEY_PEM",
    "VOICE_HELLO_PUBLIC_KEY_PEM",
    "VOICE_RTC_BRIDGE_CREDENTIAL",
    "VOICE_RTC_BRIDGE_CERT_BINDING",
    "VOICE_GATEWAY_SHARED_ASSERTION",
)

for _name in HELLO_CAPABILITY_ENV:
    if not os.environ.get(_name):
        os.environ[_name] = f"backend-test-only-{_name.lower()}"
