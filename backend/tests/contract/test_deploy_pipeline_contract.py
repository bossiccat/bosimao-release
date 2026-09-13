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
from pathlib import Path

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


def test_workflow_is_valid_yaml_and_deploys_both_services() -> None:
    doc = yaml.safe_load(_text())
    assert doc["name"] == "deploy-cloudrun"
    services = {entry["service"] for entry in _matrix()}
    assert services == {"jax-voice-api", "jax-voice-bridge"}


def test_every_service_builds_from_a_canonical_dockerfile() -> None:
    entries = {e["service"]: e for e in _matrix()}
    assert entries["jax-voice-api"]["dockerfile"] == "cloudapi/Dockerfile"
    assert entries["jax-voice-bridge"]["dockerfile"] == "cloudbridge/Dockerfile"
    text = _text()
    assert '-f "${DOCKERFILE}"' in text, "必须用矩阵里的 canonical Dockerfile 构建"
    assert "-t \"${TCR_REGISTRY}/${TCR_NAMESPACE}/${SERVICE}:${GITHUB_SHA}\"" in text


def test_image_tag_is_immutable_commit_sha() -> None:
    """tag 必须绑定 commit，不能是 latest —— 否则无法回滚也无法审计。"""
    text = _text()
    assert ":${GITHUB_SHA}" in text
    assert ":latest" not in text


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
