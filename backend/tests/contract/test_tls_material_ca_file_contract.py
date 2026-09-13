"""契约：CA 可由「镜像内既存文件」提供，而非必须走环境变量 PEM。

为什么需要这条契约
------------------
CloudRun 对 EnvParam 有 **5KB 上限**（实测报错原文：`环境变量长度超长，最长支持5kb`）。
公共 CA bundle 有 11.9KB，若强制把 CA 也塞进环境变量，bridge 的 env 总长会到 16.8KB 直接
被平台拒绝。而 python:3.11-slim 镜像本身已装 `ca-certificates`，正确的做法是让
`RTC_BRIDGE_CONTROL_PLANE_CA_FILE` 直接指向 `/etc/ssl/certs/ca-certificates.crt`。

这不是放松校验：CA 来源变了，但「CA 必须存在」仍然是 fail-closed 的——两条来源皆空即拒绝。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cloudbridge"))

import tls_material  # noqa: E402

CERT = "-----BEGIN CERTIFICATE-----\nFAKE-CERT\n-----END CERTIFICATE-----\n"  # 测试假值
KEY = "-----BEGIN PRIVATE KEY-----\nFAKE-KEY\n-----END PRIVATE KEY-----\n"  # 测试假值
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"


def test_ca_via_image_file_does_not_write_ca_and_keeps_env_value(tmp_path):
    """CA 走镜像内文件时：只落 cert/key，不落 ca.crt，也不改写 CA_FILE。"""
    out = tls_material.materialize_tls_files(
        tmp_path,
        {
            tls_material.CLIENT_CERT_PEM: CERT,
            tls_material.CLIENT_KEY_PEM: KEY,
            tls_material.CONTROL_PLANE_CA_FILE: SYSTEM_CA,
        },
    )
    assert set(out) == {tls_material.CLIENT_CERT_FILE, tls_material.CLIENT_KEY_FILE}
    assert out[tls_material.CLIENT_CERT_FILE].endswith("client.crt")
    assert out[tls_material.CLIENT_KEY_FILE].endswith("client.key")
    assert not (Path(tmp_path) / "ca.crt").exists()
    # CA_FILE 未被覆盖 —— 调用方原有取值必须原样生效
    assert tls_material.CONTROL_PLANE_CA_FILE not in out


def test_ca_pem_still_materializes_three_files(tmp_path):
    """回归：CA 走 PEM 的旧路径必须仍然落三份文件。"""
    out = tls_material.materialize_tls_files(
        tmp_path,
        {
            tls_material.CLIENT_CERT_PEM: CERT,
            tls_material.CLIENT_KEY_PEM: KEY,
            tls_material.CONTROL_PLANE_CA_PEM: CERT,
        },
    )
    assert set(out) == {
        tls_material.CLIENT_CERT_FILE,
        tls_material.CLIENT_KEY_FILE,
        tls_material.CONTROL_PLANE_CA_FILE,
    }
    assert (Path(tmp_path) / "ca.crt").is_file()


def test_missing_ca_both_sources_is_fail_closed(tmp_path):
    """CA 两条来源皆空 → 拒绝，且错误信息只列键名、不含 PEM 内容。"""
    with pytest.raises(tls_material.TlsMaterialError) as exc:
        tls_material.materialize_tls_files(
            tmp_path,
            {tls_material.CLIENT_CERT_PEM: CERT, tls_material.CLIENT_KEY_PEM: KEY},
        )
    text = str(exc.value)
    assert tls_material.CONTROL_PLANE_CA_PEM in text
    assert tls_material.CONTROL_PLANE_CA_FILE in text
    assert "FAKE-CERT" not in text and "FAKE-KEY" not in text
    assert not any(Path(tmp_path).iterdir())


def test_all_pem_empty_returns_empty_and_writes_nothing(tmp_path):
    """全空 = 未配置：返回 {} 且不创建任何文件（由下游 fail-closed 处理）。"""
    assert tls_material.materialize_tls_files(tmp_path, {}) == {}
    assert not any(Path(tmp_path).iterdir())
