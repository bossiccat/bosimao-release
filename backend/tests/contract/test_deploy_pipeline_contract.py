"""云端部署流水线的契约（防止它悄悄退化回"本地脚本部署"）。

背景：部署此前是在本机 shell 里手工完成的——组装源码目录、用 CLI 上传、再靠人看
日志判断成败。这条流水线把三件事搬到云端并固定下来：

1. 构建发生在 CI runner，镜像 tag = commit SHA（不可变、可回滚）；
2. 运行时凭据不落在仓库或 CI，只存在于 CloudRun 服务配置；
3. 部署成败由**产品自身**的探针判定（存储分步探针 + 控制面写入自检），不看日志。

本文件把这三条变成可执行断言，避免以后有人图省事改回本地部署。
"""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-jax-voice-api.yml"

# 本地/绕行手段：这些词不应出现在云端部署流水线里
FORBIDDEN_LOCAL_TOKENS = (
    "adb reverse",
    "adb connect",
    "localhost",
    "127.0.0.1",
    "tailscale",
    "jax-watchdog",
    "install-scheduled-tasks",
)


def _text() -> str:
    assert WORKFLOW.is_file(), "缺少云端部署流水线 deploy-jax-voice-api.yml"
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_is_valid_yaml_and_defined() -> None:
    doc = yaml.safe_load(_text())
    assert doc["name"] == "deploy-jax-voice-api"
    assert "deploy" in doc["jobs"]


def test_image_is_built_from_the_canonical_dockerfile_with_repo_root_context() -> None:
    text = _text()
    assert "-f cloudapi/Dockerfile" in text, "必须用 canonical Dockerfile 构建"
    assert "-t \"${TCR_REGISTRY}/${TCR_NAMESPACE}/${IMAGE_REPO}:${GITHUB_SHA}\"" in text


def test_image_tag_is_immutable_commit_sha() -> None:
    """tag 必须绑定 commit，不能是 latest —— 否则无法回滚也无法审计。"""
    text = _text()
    assert ":${GITHUB_SHA}" in text
    assert ":latest" not in text


def test_deploy_uses_image_and_attaches_vpc_with_exact_field_names() -> None:
    text = _text()
    assert "--imageUrl" in text, "必须按镜像部署（不再从本机组装源码上下文）"
    # CLI 要求四个字段且大小写精确（vpcCIDR/subnetCIDR 不能写成 vpcCidr）；
    # 且只有这条命令会真正把 VpcConf 落盘。
    for field in ("vpcId", "vpcCIDR", "subnetId", "subnetCIDR"):
        assert field in text, f"vpcConfig 缺少字段 {field}"
    assert "vpcCidr" not in text and "subnetCidr" not in text, "字段名大小写错误，CLI 会忽略"


def test_gate_is_decided_by_the_product_not_by_logs() -> None:
    """成败判定必须来自服务自身的两个探针。"""
    text = _text()
    assert "/api/v1/voice/cloud/status" in text
    assert "/api/v1/voice/cloud/selfcheck" in text
    assert "storage" in text and "selfcheck" in text


def test_preflight_fails_closed_when_configuration_is_missing() -> None:
    """缺配置必须直接失败，不允许带病部署。"""
    text = _text()
    assert "Preflight" in text
    assert "exit 1" in text


def test_only_public_access_types_are_configured() -> None:
    assert "MINIAPP" not in _text()  # 不为非必要入口开权限


def test_no_local_or_bypass_mechanisms_in_pipeline() -> None:
    lowered = _text().lower()
    for token in FORBIDDEN_LOCAL_TOKENS:
        assert token not in lowered, f"云端部署流水线不得出现本地/绕行手段: {token}"


def test_runtime_secrets_are_not_hardcoded() -> None:
    """运行时凭据（DSN / 密钥）不得写进流水线，只允许来自 CloudRun 服务配置与 secrets。"""
    text = _text()
    for leak in ("postgresql://", "VOICE_DATABASE_URL=", "TRTC_SECRETKEY="):
        assert leak not in text, f"流水线内出现硬编码凭据: {leak}"
