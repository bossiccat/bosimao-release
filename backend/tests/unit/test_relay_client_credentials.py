"""relay_client 凭据通道安全测试：密钥只允许 env，禁止 argv。"""
from __future__ import annotations

import pytest

from relay.relay_client import build_parser, resolve_credentials


def test_parser_rejects_token_flag():
    """--token 必须被 parser 拒绝（凭据走 env，杜绝 WMI/argv 泄露面）。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--relay", "ws://x", "--pairing-code", "123456", "--token", "secret"]
        )


def test_parser_rejects_e2ee_key_flag():
    """--e2ee-key 必须被 parser 拒绝。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--relay", "ws://x", "--pairing-code", "123456", "--e2ee-key", "secret"]
        )


def test_help_does_not_advertise_secret_flags():
    """帮助文本不得出现密钥旗标（避免引导运维走 argv）。"""
    help_text = build_parser().format_help()
    assert "--token" not in help_text
    assert "--e2ee-key" not in help_text


def test_credentials_resolve_from_env():
    """token/e2ee-key 从 env 读取，strip 空白。"""
    token, key = resolve_credentials(
        {"RELAY_TOKEN": " t1 ", "RELAY_E2EE_KEY": " k1 ", "OTHER": "x"}
    )
    assert token == "t1"
    assert key == "k1"


def test_credentials_default_empty_without_env():
    """env 缺失时返回空串（空 token = 明文模式降级路径，行为保持）。"""
    token, key = resolve_credentials({})
    assert token == ""
    assert key == ""
