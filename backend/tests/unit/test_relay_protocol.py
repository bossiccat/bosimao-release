"""中继协议单测（M2）：帧编解码 / 配对帧 / E2EE / AAD 防重放

覆盖 mobile-voice-spec §7 帧格式 + M2 扩展：pair 帧、AES-256-GCM 加解密、
AAD 含 seq 防重放/防篡改、ReplayGuard 拒绝非递增 seq。
"""
from __future__ import annotations

import base64

import pytest

from relay.relay_protocol import (
    AUDIO_FRAME_HEADER_LEN,
    RelayE2EE,
    ReplayGuard,
    decode_audio_frame,
    encode_audio_frame,
    gen_dev_key_b64,
    is_audio_frame,
    load_e2ee_key,
    make_pair_frame,
    parse_pair_frame,
)

KEY = b"K" * 32


# ---------- 帧编解码 ----------
def test_audio_frame_roundtrip():
    data = encode_audio_frame(7, 123456789, b"pcm-payload")
    assert data[0] == 0x02
    assert len(data) == AUDIO_FRAME_HEADER_LEN + len(b"pcm-payload")
    assert is_audio_frame(data)
    chunk = decode_audio_frame(data)
    assert chunk.seq == 7
    assert chunk.ts_ms == 123456789
    assert chunk.payload == b"pcm-payload"


def test_audio_frame_decode_errors():
    with pytest.raises(ValueError):
        decode_audio_frame(b"\x02\x00")  # 过短
    with pytest.raises(ValueError):
        decode_audio_frame(b"\x99" + b"\x00" * 20)  # magic 错误
    assert not is_audio_frame(b"\x99" + b"\x00" * 20)


# ---------- 配对帧 ----------
def test_pair_frame_roundtrip():
    raw = make_pair_frame("phone", "samsung-s24", "123456")
    msg = parse_pair_frame(raw)
    assert msg["type"] == "pair"
    assert msg["role"] == "phone"
    assert msg["device_id"] == "samsung-s24"
    assert msg["pairing_code"] == "123456"


def test_pair_frame_with_token():
    raw = make_pair_frame("pc", "jax-pc-01", "654321", token="secret")
    msg = parse_pair_frame(raw)
    assert msg["token"] == "secret"


def test_pair_frame_validation():
    with pytest.raises(ValueError):
        make_pair_frame("bogus", "d", "123456")
    with pytest.raises(ValueError):
        parse_pair_frame('{"type":"hello","role":"phone"}')
    with pytest.raises(ValueError):
        parse_pair_frame('{"type":"pair","role":"phone","device_id":"d"}')  # 缺 pairing_code
    with pytest.raises(ValueError):
        parse_pair_frame("not json")


# ---------- E2EE ----------
def test_e2ee_roundtrip():
    e2ee = RelayE2EE(KEY)
    enc = e2ee.encrypt_audio(1, 1000, b"pcm-data")
    assert enc != b"pcm-data"
    assert e2ee.decrypt_audio(1, 1000, enc) == b"pcm-data"


def test_e2ee_aad_binds_seq_and_ts():
    """AAD 含 seq/ts：换 seq 或 ts 解密失败（防重放/防篡改）"""
    e2ee = RelayE2EE(KEY)
    enc = e2ee.encrypt_audio(5, 2000, b"pcm-data")
    with pytest.raises(Exception):
        e2ee.decrypt_audio(6, 2000, enc)  # seq 不同
    with pytest.raises(Exception):
        e2ee.decrypt_audio(5, 2001, enc)  # ts 不同


def test_e2ee_key_validation():
    with pytest.raises(ValueError):
        RelayE2EE(b"short")
    with pytest.raises(ValueError):
        load_e2ee_key("not-base64!!")


def test_load_e2ee_key_base64_roundtrip():
    b64 = gen_dev_key_b64()
    key = load_e2ee_key(b64)
    assert len(key) == 32
    # base64 编码后再解码必须一致
    assert base64.b64decode(b64) == key


# ---------- ReplayGuard ----------
def test_replay_guard_monotonic():
    g = ReplayGuard()
    g.check("up", 1)
    g.check("up", 2)
    g.check("up", 3)
    with pytest.raises(ValueError):
        g.check("up", 3)  # 重放
    with pytest.raises(ValueError):
        g.check("up", 2)  # 乱序


def test_replay_guard_directions_independent():
    g = ReplayGuard()
    g.check("up", 10)
    g.check("down", 10)  # 不同方向互不影响
    with pytest.raises(ValueError):
        g.check("up", 10)
    g.check("down", 11)
