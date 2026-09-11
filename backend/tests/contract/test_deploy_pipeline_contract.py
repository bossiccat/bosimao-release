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

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudrun.yml"

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
