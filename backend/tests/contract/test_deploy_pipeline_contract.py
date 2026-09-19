"""云端部署流水线的契约（防止它悄悄退化回"本地脚本部署"）。

背景：部署此前是在本机 shell 里手工完成的——组装源码目录、用 CLI 上传、再靠人看
日志判断成败。这条流水线把三件事搬到云端并固定下来：

1. 构建发生在 CI runner，镜像 tag = commit SHA（不可变、可回滚）；
2. 运行时凭据不落在仓库或 CI，只存在于 CloudRun 服务配置；
3. 部署成败由**产品自身**的探针判定，不看日志。

2026-09-11 起流水线覆盖两个服务：控制面 `jax-voice-api` 与云端音频对端
`jax-voice-bridge`（后者由本地 PC 的 sidecar + rtc_bridge 迁移而来）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudrun.yml"
CLOUDAPI_DOCKERFILE = ROOT / "cloudapi" / "Dockerfile"

# 每个服务在 preflight 里必须校验、并在部署前注入的运行时键（单一真源）。
# preflight 与注入都遍历 workflow 矩阵里的 required_env，因此这里与 workflow
# 必须一一对应；任何一侧加了键而另一侧没加，下面的测试会失败。
EXPECTED_SERVICE_ENV = {
    "jax-voice-api": {
        "VOICE_PRODUCTION",
        "VOICE_STORAGE_BACKEND",
        "VOICE_DATABASE_URL",
        "VOICE_TLS_ENABLED",
        "RTC_TERMINATION_ENABLED",
        "VOICE_OWNER_CREDENTIAL",
        "VOICE_SIDECAR_CREDENTIAL",
        "TRTC_SDKAPPID",
        "TRTC_SECRETKEY",
        "VOICE_HELLO_PRIVATE_KEY_PEM",
        "VOICE_HELLO_PUBLIC_KEY_PEM",
        "VOICE_RTC_BRIDGE_CERT_BINDING",
        "VOICE_RTC_BRIDGE_CREDENTIAL",
        "VOICE_GATEWAY_SHARED_ASSERTION",
        "VOICE_TRUSTED_GATEWAY_HOSTS",
    },
    "jax-voice-bridge": {
        "CONTROL_PLANE_BASE_URL",
        "VOICE_SIDECAR_CREDENTIAL",
        "TRTC_SDKAPPID",
        "TRTC_SECRETKEY",
        "QWEN_REALTIME_WS_URL",
        "QWEN_REALTIME_API_KEY",
        "RTC_BRIDGE_CONTROL_PLANE_BASE_URL",
        "RTC_BRIDGE_SERVICE_CREDENTIAL",
        "RTC_BRIDGE_GATEWAY_ASSERTION",
        # 容器内实际注入的是 **PEM 内容**，不可能是路径：路径由 cloudbridge/tls_material.py
        # 启动时把 PEM 落成 0o600 临时文件后自己生成。原清单写的是 *_FILE（路径），属真错误。
        "RTC_BRIDGE_CLIENT_CERT_PEM",
        "RTC_BRIDGE_CLIENT_KEY_PEM",
        "RTC_BRIDGE_CONTROL_PLANE_CA_FILE",
        # 幽灵键已移除：`RTC_BRIDGE_CERT_BINDING` 在 rtc_bridge 的配置里**不存在**
        # （`VOICE_RTC_BRIDGE_CERT_BINDING` 是 api 侧配置），没有任何运行时代码消费它。
    },
}

# 本地/绕行手段：这些词不应出现在云端部署流水线里
FORBIDDEN_LOCAL_TOKENS = (
    "adb reverse",
    "adb connect",
    "localhost",
    "127.0.0.1",
    "tailscale",
    "jax-watchdog",
    "install-scheduled-tasks",
    "start-all.ps1",
)


def _text() -> str:
    assert WORKFLOW.is_file(), "缺少云端部署流水线 deploy-cloudrun.yml"
    return WORKFLOW.read_text(encoding="utf-8")


def _matrix() -> list[dict]:
    doc = yaml.safe_load(_text())
    return doc["jobs"]["deploy"]["strategy"]["matrix"]["include"]


def _doc() -> dict:
    return yaml.safe_load(_text())


def _steps() -> list[dict]:
    return _doc()["jobs"]["deploy"]["steps"]


def _step(name: str) -> dict:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"workflow 里找不到步骤: {name}")


def _step_order() -> dict[str, int]:
    """步骤名 → 在 steps 中的下标（用于断言相对顺序，而不是靠字符串位置）。"""
    return {s["name"]: i for i, s in enumerate(_steps()) if "name" in s}


def _command_lines(step_name: str) -> str:
    """步骤 shell 脚本里**去掉注释行**后的内容。

    注释里可以提到某个包名（比如「刻意不装 windows-capture」），那不算安装清单的一部分。
    """
    run = _step(step_name)["run"]
    return "\n".join(line for line in run.splitlines() if not line.strip().startswith("#"))


def _header_comment() -> str:
    """文件顶部的注释块（YAML 注释不会被 yaml.safe_load 保留，只能读原文）。"""
    out: list[str] = []
    for line in _text().splitlines():
        if line.startswith("#"):
            out.append(line)
            continue
        if not out:
            continue
        if line.strip() == "":
            out.append(line)
            continue
        break
    return "\n".join(out)


def test_workflow_is_valid_yaml_and_deploys_both_services() -> None:
    doc = yaml.safe_load(_text())
    assert doc["name"] == "deploy-cloudrun"
    services = {entry["service"] for entry in _matrix()}
    assert services == {"jax-voice-api", "jax-voice-bridge"}


def test_every_service_builds_from_a_canonical_dockerfile() -> None:
    entries = {e["service"]: e for e in _matrix()}
    assert entries["jax-voice-api"]["dockerfile"] == "cloudapi/Dockerfile"
    assert entries["jax-voice-bridge"]["dockerfile"] == "cloudbridge/Dockerfile"
    run = _step("Build canonical image (context = repo root)")["run"]
    assert "docker build" in run
    assert '-f "${DOCKERFILE}"' in run, "必须用矩阵里的 canonical Dockerfile 构建"
    assert ":${COMMIT}" in run, "镜像 tag 必须用解析后的提交（见提交标识一致性测试）"


def test_image_tag_is_immutable_commit_sha() -> None:
    """tag 必须绑定 commit，不能是 latest —— 否则无法回滚也无法审计。

    注意这里是「绑到实际检出的那个提交」，而不是绑到 GITHUB_SHA：后者在
    workflow_dispatch 指定 ref 时只是触发分支的 tip，会与镜像内容不一致。
    """
    text = _text()
    assert ":latest" not in text
    assert "${GITHUB_SHA}" not in text, \
        "不得用 ${GITHUB_SHA} 充当镜像/部署标识（它与实际检出的提交可能是两个东西）"
    assert ":${COMMIT}" in text


def test_deploy_uses_image_and_attaches_vpc_with_exact_field_names() -> None:
    text = _text()
    assert "--imageUrl" in text, "必须按镜像部署（不再从本机组装源码上下文）"
    for field in ("vpcId", "vpcCIDR", "subnetId", "subnetCIDR"):
        assert field in text, f"vpcConfig 缺少字段 {field}"
    assert "vpcCidr" not in text and "subnetCidr" not in text, "字段名大小写错误，CLI 会忽略"


def test_bridge_container_port_is_not_the_platform_reserved_one() -> None:
    """CloudRun 明确禁用 9100（must not be 9100），音频桥用 9200。"""
    ports = {e["service"]: e["port"] for e in _matrix()}
    assert ports["jax-voice-bridge"] == 9200
    assert ports["jax-voice-api"] == 9000
    assert 9100 not in ports.values()


def test_gate_is_decided_by_each_product_itself() -> None:
    """控制面看存储探针 + 自检；音频桥看两个子进程与 rtc_bridge 健康。"""
    text = _text()
    assert "/api/v1/voice/cloud/status" in text
    assert "/api/v1/voice/cloud/selfcheck" in text
    assert "/api/v1/voice/bridge/status" in text
    gates = {e["gate"] for e in _matrix()}
    assert gates == {"api", "bridge"}


def test_preflight_fails_closed_when_configuration_is_missing() -> None:
    text = _text()
    assert "Preflight" in text
    assert "exit 1" in text


def test_no_local_or_bypass_mechanisms_in_pipeline() -> None:
    lowered = _text().lower()
    for token in FORBIDDEN_LOCAL_TOKENS:
        assert token not in lowered, f"云端部署流水线不得出现本地/绕行手段: {token}"


def test_runtime_secrets_are_not_hardcoded() -> None:
    """运行时凭据（DSN / 密钥）不得写进流水线，只允许来自 CloudRun 服务配置与 secrets。"""
    text = _text()
    for leak in ("postgresql://", "VOICE_DATABASE_URL=", "TRTC_SECRETKEY=", "sk-ws-"):
        assert leak not in text, f"流水线内出现硬编码凭据: {leak}"


def test_retired_single_service_workflow_is_gone() -> None:
    """两个服务已经合并到矩阵流水线，旧的单服务文件不得复活。"""
    assert not (ROOT / ".github" / "workflows" / "deploy-jax-voice-api.yml").exists()


# ---------------------------------------------------------------------------
# 2026-09-11 起的加固：把"部署配置"固化进仓库，并让 CI 在缺配置时 fail-closed。
# 背景地雷：VOICE_PRODUCTION 未固化会让 store_factory 静默回退容器内 SQLite；
# VpcConf 只存在于云端控制台，整包覆盖式发布会丢掉它。
# ---------------------------------------------------------------------------

def test_api_image_bakes_the_production_switch() -> None:
    """VOICE_PRODUCTION=true 必须固化在镜像里。

    app/voice/store_factory.py:84-94 按 settings.voice_production 分支：False 时
    装配容器内 SQLite 夹具（数据易失）。只靠云端 EnvParams 注入时，任何不带
    EnvParams 的部署都会静默回退 SQLite——这正是要固化的原因。
    """
    text = CLOUDAPI_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"(?im)^\s*ENV\s+VOICE_PRODUCTION\s*=\s*true\s*$", text), \
        "cloudapi/Dockerfile 必须含一行 ENV VOICE_PRODUCTION=true"
    assert not re.search(r"(?i)VOICE_PRODUCTION\s*[=:]\s*(?:false|0|no)\b", text), \
        "生产镜像不得把 VOICE_PRODUCTION 关掉"


def test_api_deploy_step_keeps_production_switch_and_vpc() -> None:
    """api 的部署路径必须注入 VOICE_PRODUCTION，且必须保留 --vpcConfig。"""
    text = _text()
    assert "VOICE_PRODUCTION" in text, "api 部署路径必须注入 VOICE_PRODUCTION"
    assert "--vpcConfig" in text, "api 部署步骤必须保留 --vpcConfig（否则连不到内网 PG）"


def test_preflight_manifest_is_the_single_source_of_injection() -> None:
    """preflight 校验与注入必须共用同一份 required_env，且每个键都有来源声明。

    这同时防止两种漂移：加了注入却忘了加校验，或反之。
    """
    text = _text()
    assert text.count("${{ matrix.required_env }}") >= 2, \
        "preflight 校验与注入必须都遍历 matrix.required_env（单一真源）"
    entries = {e["service"]: e for e in _matrix()}
    assert set(entries) == set(EXPECTED_SERVICE_ENV), "矩阵服务集合变了，需同步更新必需键清单"
    job_env = yaml.safe_load(text)["jobs"]["deploy"].get("env", {}) or {}
    for service, expected in EXPECTED_SERVICE_ENV.items():
        declared = set(entries[service].get("required_env", "").split())
        assert declared == expected, f"{service} required_env 与预期必填清单不一致"
        for key in sorted(expected):
            assert key in job_env, f"{key} 在 required_env 里但没有来源声明（会取到空值）"


# ── 基础设施标识的存放通道（2026-09-18 修正）───────────────────────────────────
# VPC id / 子网 id / 网段这四项是**内网拓扑标识**，已从全部提交历史中清理掉。
# 本仓库是公开仓库：GitHub variables 不加密，且其机密性无法验证（匿名读 Actions
# variables 返回 401，但没有任何证据表明已认证的非协作者读不到）。若这四项只从
# vars 取，等于把刚清理掉的标识又放回一个机密性不可验证的通道——清理白做。
# 因此它们必须经 secrets 取（允许回退同名 vars，但 secrets 优先）。
INFRA_IDENTIFIER_KEYS = ("VPC_ID", "VPC_CIDR", "SUBNET_ID", "SUBNET_CIDR")


def test_infra_identifiers_are_sourced_from_secrets_not_vars() -> None:
    """四项基础设施标识不得只从 vars 取，必须先经 secrets。"""
    job_env = _doc()["jobs"]["deploy"].get("env", {}) or {}
    for key in INFRA_IDENTIFIER_KEYS:
        assert key in job_env, f"{key} 没有来源声明"
        declared = str(job_env[key])
        assert declared.startswith("${{ secrets."), (
            f"{key} 必须先取 secrets（公开仓库的 variables 不加密，"
            f"机密性不可验证）: {declared}")
        assert "vars." in declared, (
            f"{key} 应保留同名 Vars 回退，与其余运行时键的写法一致: {declared}")


_HARDCODED_SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?://", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(
        r"(?i)\b(?:VOICE_DATABASE_URL|TRTC_SECRETKEY|TRTC_SDKAPPID|"
        r"VOICE_OWNER_CREDENTIAL|VOICE_SIDECAR_CREDENTIAL|"
        r"VOICE_HELLO_PRIVATE_KEY_PEM|VOICE_HELLO_PUBLIC_KEY_PEM|"
        r"VOICE_RTC_BRIDGE_CREDENTIAL|VOICE_GATEWAY_SHARED_ASSERTION|"
        r"QWEN_REALTIME_API_KEY|RTC_BRIDGE_SERVICE_CREDENTIAL|"
        r"RTC_BRIDGE_GATEWAY_ASSERTION)\s*[:=]\s*[A-Za-z0-9+/=_\-]{16,}"
    ),
)


def test_pipeline_carries_no_literal_secret_values() -> None:
    """结构性地断言流水线里没有任何键被直接赋了字面量值（只允许 ${{ }} 引用）。"""
    text = _text()
    for pattern in _HARDCODED_SECRET_PATTERNS:
        assert not pattern.search(text), \
            f"流水线内出现疑似硬编码密钥值，模式: {pattern.pattern}"


def test_production_switch_is_never_disabled_or_dropped() -> None:
    text = _text()
    assert not re.search(r"(?i)VOICE_PRODUCTION\s*[=:]\s*(?:false|0|no)\b", text), \
        "流水线里不得出现 VOICE_PRODUCTION=false"
    assert not re.search(r"(?im)VOICE_PRODUCTION\s*[:=]\s*$", text), \
        "不得把 VOICE_PRODUCTION 置空"
    entries = {e["service"]: e for e in _matrix()}
    assert "VOICE_PRODUCTION" in entries["jax-voice-api"]["required_env"].split(), \
        "VOICE_PRODUCTION 不能被移出 api 的必填清单"


# ── CI env 注入机制（2026-09-12 修正）─────────────────────────────────────────
# 原实现用 `tcb secrets set`：那是**环境级**通道，而这里要注入的是**服务级**运行时配置，
# 写进去不会落到本服务的 EnvParams（旧版 CLI 甚至没有该子命令）。唯一可靠通道是 tcbr 原生
# API `SubmitServerConfigChangeDiff`，且 `EnvParam` 是**整表覆盖**，必须先读回再合并。
# 这条测试守住「不许退回错误通道」，也守住「必须读回自证」。

def test_ci_injects_env_through_tcbr_api_not_secrets_set() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "SubmitServerConfigChangeDiff" in text, "必须走 tcbr 原生 API 注入"
    assert "EnvParam" in text, "写入侧字段名是 EnvParam（单数，读回才是 EnvParams）"
    # 只断言**可执行形式**消失：注释里提到该通道是正常的（正是在说明它为什么不能用）
    assert 'secrets set "$name"' not in text, "secrets 是环境级通道，不得用于服务级运行时配置"


def test_ci_injection_reads_back_and_fails_closed() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # 注入后必须读回比对必需键（只比键名），缺失即非零退出
    assert "DescribeCloudRunServerDetail" in text
    assert "注入后读回缺键" in text, "缺少读回自证"


def test_ci_does_not_require_ghost_bridge_cert_binding() -> None:
    """`RTC_BRIDGE_CERT_BINDING` 无任何消费者（bridge 侧没有这个键），不得再进必填清单。

    必须精确匹配独立 token：`VOICE_RTC_BRIDGE_CERT_BINDING` 是合法且必需的 api 侧配置，
    子串匹配会把它误报成幽灵键（我第一次就写错了，被测试当场抓出）。
    """
    import re
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"(?<!VOICE_)RTC_BRIDGE_CERT_BINDING", text)


# ── 2026-09-13 加固：部署前测试门禁 / 提交标识统一 / 已知缺口显式化 ──────────────
#
# 审计确认的三个缺陷（本段逐条锁死）：
#   1. 全流程唯一的门禁是**部署之后**的 HTTP 探针，一行测试都没跑；
#   2. checkout 用 `github.event.inputs.ref || github.sha` 决定检出，而镜像 tag / 部署用
#      `${GITHUB_SHA}` —— 手动指定 ref 时两者指向不同提交，镜像内容与 tag 对不上；
#   3. 「部署先于验证、失败不回滚」既没有回滚也没有写下来，属于隐性行为。
#
# 断言策略：先解析 YAML 拿结构（顺序、env 映射、有无 if/continue-on-error），再补文件
# 系统事实（被引用的目录/文件是否真的存在），最后有一处真正执行脚本的行为断言。

_GATE_BACKEND = "Pre-deploy gate - backend contract tests"
_GATE_SIDECAR = "Pre-deploy gate - sidecar node tests"
_TEST_DEPS = "Install pre-deploy test dependencies"
_BUILD_STEP = "Build canonical image (context = repo root)"
_PUSH_STEP = "Push image to TCR"
_DEPLOY_STEP = "Deploy image with VPC attachment"
_VERIFY_STEP = "Verify deployment through the product itself"

# sidecar 里确实需要真机 electron / trtc-electron-sdk 的用例（Windows 专属 electron.exe）。
# 这份清单不是手写的期望，而是由 test_sidecar_gate_excludes_only_electron_dependent_files
# 从文件内容里**推**出来再比对的——新增同类用例时会立刻失败。
_EXPECTED_SIDECAR_EXCLUSIONS = ("audio-contract", "exit-lifecycle", "sdk-smoke")

# 只在 Windows / 需要原生编解码时可用，ubuntu runner 上必须**不出现**在安装清单里。
_WINDOWS_ONLY_PACKAGES = (
    "windows-capture",
    "pywin32",
    "sounddevice",
    "silero-vad",
    "sherpa-onnx",
)


def test_pre_deploy_gate_runs_before_the_build() -> None:
    """测试门禁必须排在 build 之前：建好镜像再测就没意义了。"""
    order = _step_order()
    assert _BUILD_STEP in order
    build_at = order[_BUILD_STEP]
    for name in (_GATE_BACKEND, _GATE_SIDECAR):
        assert name in order, f"缺少部署前测试门禁步骤: {name}"
        assert order[name] < build_at, f"{name} 必须排在 {_BUILD_STEP} 之前"


def test_pre_deploy_gate_has_no_bypass_path() -> None:
    """不存在「测试没跑但部署照走」的路径。

    两条：门禁步骤不得带 if（可被条件跳过）或 continue-on-error（失败被吞掉）。
    顺带把这条不变量推广到**所有**步骤——目前确实如此，保持住就不可能出现旁路。

    2026-09-19 收窄（是收窄，不是放松）：部署后新增的 VpcConf 回读步骤**必须**带
    `if: always()` 才能做到「Verify 失败时仍然执行」——它不能改成省略 if，否则前序
    失败时它会被跳过，这条最关键的守卫反而变成**静默缺席**。于是不变量精确化为：
      · 带 if 的步骤**只能**位于 Deploy 之后。门禁、构建、注入、部署全在它前面，
        所以它不可能跳过其中任何一步，也就无法制造旁路；
      · 且 if 只能恰好是 `always()`（一个不可能跳过它的条件），不得是任何可跳过它的条件。
    原不变量「不存在测试没跑却照常部署的路径」完整保留，并额外新增
    「回读不得缺席」这一条。
    """
    order = _step_order()
    deploy_at = order[_DEPLOY_STEP]
    for index, step in enumerate(_steps()):
        label = step.get("name") or step.get("uses", "<unnamed>")
        assert not step.get("continue-on-error"), f"{label} 吞掉了失败：后续步骤会照走"
        if "if" not in step:
            continue
        assert index > deploy_at, (
            f"{label} 带了 if 且不在 {_DEPLOY_STEP} 之后：可能跳过构建、门禁、注入或部署")
        assert str(step["if"]).strip() == "always()", (
            f"{label} 的 if 只能恰好是 always()（不得带任何可跳过它的条件）: {step['if']!r}")
    for name in (_GATE_BACKEND, _GATE_SIDECAR):
        _step(name)  # 必须真实存在，而不是被改名或删掉


def test_pre_deploy_gate_fails_closed_on_failure() -> None:
    """每个门禁步骤都要 set -euo pipefail：非零退出即中止，构建/部署不执行。"""
    for name in (_GATE_BACKEND, _GATE_SIDECAR):
        assert "set -euo pipefail" in _step(name)["run"], f"{name} 未声明 fail-closed"


def test_backend_contract_suite_is_really_invoked() -> None:
    """门禁必须真的跑 backend/tests/contract，且那个目录里确实有用例（不能空转）。"""
    run = _step(_GATE_BACKEND)["run"]
    assert "python -m pytest backend/tests/contract" in run

    contract_dir = ROOT / "backend" / "tests" / "contract"
    assert contract_dir.is_dir(), "契约套件目录不存在"
    cases = sorted(contract_dir.glob("test_*.py"))
    assert len(cases) >= 20, f"契约套件文件数异常：{len(cases)}"
    assert all("def test_" in p.read_text(encoding="utf-8") for p in cases)


def test_test_dependencies_are_linux_installable_and_precede_the_gate() -> None:
    """依赖安装排在门禁之前，且只装 ubuntu 上装得上的包。

    实测背景（2026-09-13，干净 3.11 venv）：`backend/tests/contract` 会经
    `app.main → core.orchestrator → capture.*` 拉起整个后端 import 闭包，最少需要
    fastapi / httpx / numpy / pillow / psutil / pydantic-settings / PyYAML / jsonschema /
    openapi-* / cryptography / PyJWT / uvicorn / websockets / pytest(-asyncio)。
    而仓库 requirements.txt 里的 windows-capture / pywin32 / sounddevice / silero-vad /
    sherpa-onnx 在 ubuntu runner 上装不上——混进来门禁会直接红在安装步骤上。
    """
    order = _step_order()
    assert _TEST_DEPS in order
    assert order[_TEST_DEPS] < min(order[n] for n in (_GATE_BACKEND, _GATE_SIDECAR))

    commands = _command_lines(_TEST_DEPS)
    assert "python -m pip install" in commands
    for required in (
        "pytest==",
        "fastapi==",
        "httpx==",
        "numpy==",
        "pillow==",
        "psutil==",
        "pydantic-settings==",
        "PyYAML==",
        "cryptography==",
        "PyJWT==",
        "jsonschema==",
    ):
        assert required in commands, f"缺少运行契约套件所必需的依赖: {required}"
    for forbidden in _WINDOWS_ONLY_PACKAGES:
        assert forbidden not in commands, f"{forbidden} 在 ubuntu runner 上装不上，不得进安装清单"


def test_sidecar_gate_excludes_only_electron_dependent_files() -> None:
    """sidecar 门禁用**排除法**只放掉确实需要真机 electron 的用例。

    分两层：结构层确认用了排除法并有空集保护；文件系统层确认「被排除的文件确实依赖
    electron/node_modules」，且其余用例文件都不依赖（所以在无 node_modules 的 runner
    上可跑）。
    """
    run = _step(_GATE_SIDECAR)["run"]
    assert "node --test" in run, "sidecar 门禁必须真的跑 node --test"
    assert "exit 1" in run, "空集（一个用例都没收集到）必须非零退出（fail-closed）"

    tests = sorted((ROOT / "sidecar" / "test").glob("*.test.js"))
    assert tests, "sidecar 测试目录为空"

    needs_node_modules = {
        p.name[: -len(".test.js")]
        for p in tests
        if "node_modules" in p.read_text(encoding="utf-8")
        or "electron.exe" in p.read_text(encoding="utf-8")
    }
    assert needs_node_modules == set(_EXPECTED_SIDECAR_EXCLUSIONS), (
        "依赖 node_modules/electron 的 sidecar 用例集合变了："
        f"{sorted(needs_node_modules)} —— 必须同步更新 workflow 的排除清单"
    )
    for stem in sorted(needs_node_modules):
        assert stem in run, f"workflow 的排除清单里缺少 {stem}（不含它就是漏排除，门禁必红）"

    # 反向：其余用例文件不得引用 node_modules / electron。
    for p in tests:
        if p.name[: -len(".test.js")] in needs_node_modules:
            continue
        text = p.read_text(encoding="utf-8")
        assert "node_modules" not in text and "electron.exe" not in text, (
            f"{p.name} 既不缺依赖也没被排除：排除清单与事实不符"
        )


_SIDECAR_TREE = ROOT / "sidecar"
_BASH = shutil.which("bash")
_NODE = shutil.which("node")


@pytest.mark.skipif(
    sys.platform != "linux" or not _BASH or not _NODE,
    reason=(
        "该行为断言只在目标 runner（ubuntu-latest）上执行：Windows 上 which('bash') 命中的是 "
        "WSL 启动器，不是真正可用的 bash。缺失时由上面的静态结构断言兜底。"
    ),
)
def test_sidecar_gate_script_is_green_without_node_modules(tmp_path: Path) -> None:
    """把 workflow 里的 sidecar 门禁脚本**原样执行**一遍（行为断言，不是字符串扫描）。

    在「无 node_modules」的 sidecar 副本上跑：断言脚本真的能选出可跑的用例、全绿、并且
    显式跳过了那三个需要真机 electron 的文件。这同时验证了这段 shell 本身没有语法错误。
    """
    sandbox = tmp_path / "sidecar"
    shutil.copytree(_SIDECAR_TREE, sandbox, ignore=shutil.ignore_patterns("node_modules"))
    assert not (sandbox / "node_modules").exists()

    script = _step(_GATE_SIDECAR)["run"]
    proc = subprocess.run(
        [_BASH, "-c", script],
        cwd=str(tmp_path),  # 脚本内部自己 cd sidecar
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
    )
    assert proc.returncode == 0, f"sidecar 门禁脚本应全绿\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    for stem in _EXPECTED_SIDECAR_EXCLUSIONS:
        assert f"{stem}.test.js" in proc.stdout, f"应当显式跳过 {stem}.test.js"
    assert "running " in proc.stdout, "应当打印实际运行的用例文件数"


def test_image_and_deploy_share_one_resolved_commit_identity() -> None:
    """修掉「checkout 用 A、tag/部署用 B」的分歧。

    GITHUB_SHA 是**触发分支的 tip**；workflow_dispatch 指定 ref 时它与实际检出的提交
    不是一个东西。所以 workflow 必须在 checkout 之后立刻解析真实提交（git rev-parse HEAD），
    让 build / push / deploy 共用同一个标识。
    """
    text = _text()
    assert "${GITHUB_SHA}" not in text, "不得再用 ${GITHUB_SHA} 作为镜像/部署标识"

    resolvers = [s for s in _steps() if "git rev-parse HEAD" in (s.get("run") or "")]
    assert len(resolvers) == 1, "必须恰好有一个步骤解析真实检出的提交"
    resolver = resolvers[0]
    rid = resolver.get("id")
    assert rid, "解析提交的步骤必须有 id，后续步骤才能引用它"
    assert "$GITHUB_OUTPUT" in resolver["run"], "必须写进 GITHUB_OUTPUT 供后续步骤引用"

    m = re.search(
        r'echo\s+"([A-Za-z_][A-Za-z0-9_]*)=\$\(git rev-parse HEAD\)"', resolver["run"]
    )
    assert m, '解析步骤应形如: echo "commit=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"'
    ref = "${{ steps.%s.outputs.%s }}" % (rid, m.group(1))

    for name in (_BUILD_STEP, _PUSH_STEP, _DEPLOY_STEP):
        step = _step(name)
        declared = (step.get("env") or {}).get("COMMIT")
        assert declared == ref, f"{name} 的镜像标识必须取 {ref}，实际: {declared!r}"
        assert ":${COMMIT}" in step["run"], f"{name} 未把 COMMIT 用在镜像引用上"

    # 反向：除 checkout 之外不得再出现 github.sha，否则分歧会重新长回来。
    referencing = [
        s for s in _steps() if "github.sha" in yaml.safe_dump(s, allow_unicode=True)
    ]
    assert len(referencing) == 1, "github.sha 只应出现在 checkout 的 ref 里"
    assert str(referencing[0].get("uses", "")).startswith("actions/checkout@")


def test_no_rollback_gap_is_documented_and_ordering_matches() -> None:
    """把「部署先于验证、失败不回滚」从隐性行为变成显式已知缺口。

    两件事都要成立，缺一不可：
      · 结构性事实：Deploy 确实在 Verify 之前（这就是缺口的成因）；
      · 文档性事实：顶部注释写清了失败不回滚、以及人工回退要读哪个字段、用哪条命令。
    只写注释而顺序已经变了会误导人；顺序没变但没写下来，等于缺口仍然隐性。
    """
    order = _step_order()
    assert order[_DEPLOY_STEP] < order[_VERIFY_STEP], "预期顺序是部署先于验证"

    header = _header_comment()
    assert "不回滚" in header, "必须写明「失败不回滚」"
    assert "DescribeCloudRunServerDetail" in header, "必须写明用哪个 API 读回在线版本"
    assert "OnlineVersionInfos" in header, "必须写明从哪个字段取出上一版 revision"
    assert "tcb cloudrun deploy" in header, "必须给出人工回退的 CLI 命令"
    assert "fail-fast" in header, "必须写明 fail-fast: false 的取舍"

    assert _doc()["jobs"]["deploy"]["strategy"].get("fail-fast") is False
    # 不能偷偷加一条自动回滚步骤来「假装修好了」：回滚属于需要单独决策的另一件事。
    assert not any(
        "rollback" in str(s.get("name", "")).lower() for s in _steps()
    ), "本批不引入自动回滚；若确实要加，需先与用户确认"


# ── 部署后 VpcConf 回读（2026-09-19）──────────────────────────────────────────
# preflight 只能证明**输入**非空，证明不了管控面真的写进去了。部署步骤的 --vpcConfig
# 若被拼成「字段全空」的对象，会把 jax-voice-api **已配置**的 VpcConf 整块抹掉 →
# 控制面失去内网 PG 通路 → 控制面是端侧唯一依赖 → **产品整体不可用**。
# 这是产品级下线状态，不允许产出绿色运行，所以回读不匹配必须非零退出。
_READBACK_STEP = "Read back VpcConf and assert it matches this run"


def test_vpc_readback_exists_after_verify_and_is_loud() -> None:
    order = _step_order()
    assert _READBACK_STEP in order, "缺少部署后 VpcConf 回读步骤"
    assert order[_READBACK_STEP] > order[_VERIFY_STEP], "回读必须排在 Verify 之后"
    run = _step(_READBACK_STEP)["run"]
    assert "set -euo pipefail" in run, "回读未声明 fail-closed"
    assert "sys.exit(1)" in run, "回读不匹配必须非零退出（产品级下线状态不得产出绿色运行）"
    assert "DescribeCloudRunServerDetail" in run, "必须真的读回管控面配置"
    assert "VpcConf" in run, "读回的目标字段是 ServerConfig.VpcConf"


def test_vpc_readback_runs_even_when_verify_fails() -> None:
    """Verify 失败时，被抹掉的 VpcConf 可能正是病因：要两个信号，不是一个盖住另一个。"""
    declared = str(_step(_READBACK_STEP).get("if", "")).strip()
    assert declared == "always()", (
        "回读必须用 if: always()；省略 if 会让它在前序失败时被跳过，"
        f"这条最关键的守卫就变成静默缺席: {declared!r}")


def test_vpc_readback_compares_against_this_runs_ids_not_merely_nonempty() -> None:
    """断言「与本轮要注入的值相等」，**不是**「非空」。

    只判非空会漏掉「被写成错误但非空的值」——preflight 永远看不到那种情况。

    变异检验抓出过一版过松的写法：只断言 `"!= expected" in run` 是不够的，因为网段那
    一支也含这个子串——把两个 id 的判据弱化成「非空」后该断言仍然通过。所以这里**逐个
    精确锁定** id 的比较式本身，并锁定 id 与网段的分组不得互换。
    """
    run = _step(_READBACK_STEP)["run"]
    for key in INFRA_IDENTIFIER_KEYS:
        assert f'os.environ.get("{key}"' in run, f"回读必须取本轮的 {key} 作为期望值"
    assert 'HARD = ("vpcid", "subnetid")' in run, "两个 id 必须归在硬比较那一组"
    assert 'SOFT = ("vpccidr", "subnetcidr")' in run, "两个网段字段必须归在只判非空那一组"
    assert "got != expected[field]" in run, (
        "两个 id 必须与本轮注入值逐一相等，不能退化成只判非空")
    assert "为空（未配置）" in run, "字段缺失/为空必须单独判出来（抹掉的签名）"


def test_vpc_readback_does_not_judge_a_deploy_that_never_ran() -> None:
    """Deploy 未执行时，读回的是**部署前**的既有配置，不构成本次部署的结论。"""
    assert _step(_DEPLOY_STEP).get("id") == "deploy", \
        "Deploy 步骤必须有 id，回读才能引用它的 outcome"
    declared = (_step(_READBACK_STEP).get("env") or {}).get("DEPLOY_OUTCOME")
    assert declared == "${{ steps.deploy.outcome }}", f"实际: {declared!r}"
    assert "skipped" in _step(_READBACK_STEP)["run"], "必须显式处理 Deploy 被跳过的情况"


def test_vpc_readback_prints_only_field_names_never_values() -> None:
    """回读失败只能打印**字段名**，不得把 VPC/子网标识值打进 CI 日志。"""
    for line in _step(_READBACK_STEP)["run"].splitlines():
        stripped = line.strip()
        if not stripped.startswith("print("):
            continue
        assert "expected[" not in stripped and "norm[" not in stripped, \
            f"回读的打印语句里插入了标识值: {stripped}"


def test_vpc_readback_records_the_intended_bridge_vpc_population() -> None:
    """首次 CI 会把 bridge 原本为空的 VpcConf 变成已填充：既定意图，但必须是**已知**的。"""
    run = _step(_READBACK_STEP)["run"]
    assert "jax-voice-bridge" in run, "必须点名会发生变化的是 bridge"
    assert "原本为空" in run and "首次 CI 运行" in run, \
        "必须写明这是一次对在线服务的已知配置变更，而不是让运维撞见"
