"""中继协议单测（M2）：帧编解码 / 配对帧 / E2EE（与 App VoiceCipher 对齐）

覆盖 mobile-voice-spec §7 帧格式 + M2 扩展：pair 帧、AES-256-GCM 加解密、
AAD = seq 仅 8B 大端（App seqBytes 同构）、passphrase SHA-256 派生 / 32B base64 双表示、
payload = [iv 12B][ct+tag 16B]、ReplayGuard 拒绝非递增 seq、与 App 派生结果一致性。
"""
from __future__ import annotations

import base64
import hashlib
import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from relay.relay_protocol import (
    AUDIO_FRAME_HEADER_LEN,
    DEFAULT_E2EE_PASSPHRASE,
    E2EE_AAD_LEN,
    E2EE_NONCE_LEN,
    E2EE_TAG_LEN,
    RelayE2EE,
    ReplayGuard,
    decode_audio_frame,
    derive_key_from_passphrase,
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


def test_e2ee_payload_format_iv12_ct16():
    """payload = [iv 12B][AES-GCM 密文+tag 16B]（App VoiceCipher 同构）"""
    e2ee = RelayE2EE(KEY)
    plain = b"pcm-data"
    enc = e2ee.encrypt_audio(3, 1000, plain)
    assert len(enc) == E2EE_NONCE_LEN + len(plain) + E2EE_TAG_LEN
    # iv 每次随机 → 相同明文两次加密密文不同
    enc2 = e2ee.encrypt_audio(3, 1000, plain)
    assert enc != enc2


def test_e2ee_aad_is_seq8b_big_endian():
    """AAD = seq 仅 8 字节大端（App seqBytes 同构，u32 seq 高位补零）"""
    e2ee = RelayE2EE(KEY)
    assert E2EE_AAD_LEN == 8
    assert e2ee._aad(42) == struct.pack(">Q", 42)
    assert e2ee._aad(1) == b"\x00\x00\x00\x00\x00\x00\x00\x01"
    assert len(e2ee._aad(0xFFFFFFFF)) == 8


def test_e2ee_aad_binds_seq_only():
    """AAD 仅绑定 seq：换 seq 解密失败；ts_ms 不参与 AAD（与 App 规则一致）"""
    e2ee = RelayE2EE(KEY)
    enc = e2ee.encrypt_audio(5, 2000, b"pcm-data")
    with pytest.raises(Exception):
        e2ee.decrypt_audio(6, 2000, enc)  # seq 不同 → 失败
    assert e2ee.decrypt_audio(5, 999999, enc) == b"pcm-data"  # ts 不同 → 仍成功


def test_e2ee_key_validation():
    with pytest.raises(ValueError):
        RelayE2EE(b"short")
    with pytest.raises(ValueError):
        derive_key_from_passphrase("   ")


# ---------- 密钥：passphrase SHA-256 派生 / 32B base64 双表示 ----------
def test_derive_key_matches_app_voicecipher_and_documented_b64():
    """App VoiceCipher.deriveKey 同构：SHA-256(UTF-8)；与 OPS-003 记录的 base64 一致"""
    k = derive_key_from_passphrase(DEFAULT_E2EE_PASSPHRASE)
    assert len(k) == 32
    assert k == hashlib.sha256(DEFAULT_E2EE_PASSPHRASE.encode("utf-8")).digest()
    documented_b64 = "Q4Q/xnJEixH81+11EAyXwXTqn1+vgPMsxaWf9FQzutw="
    assert base64.b64encode(k).decode() == documented_b64


def test_load_e2ee_key_passphrase_vs_base64_equivalent():
    """load_e2ee_key 对 passphrase 与 32B base64 返回同一 32 字节密钥"""
    b64 = base64.b64encode(derive_key_from_passphrase(DEFAULT_E2EE_PASSPHRASE)).decode()
    assert load_e2ee_key(DEFAULT_E2EE_PASSPHRASE) == load_e2ee_key(b64) == derive_key_from_passphrase(DEFAULT_E2EE_PASSPHRASE)


def test_load_e2ee_key_base64_roundtrip():
    b64 = gen_dev_key_b64()
    key = load_e2ee_key(b64)
    assert len(key) == 32
    # base64 编码后再解码必须一致
    assert base64.b64decode(b64) == key


# ---------- 与 App VoiceCipher 互通（App 加密 → PC 解密 / 反向） ----------
def _app_style_encrypt(key: bytes, seq: int, plaintext: bytes) -> bytes:
    """按 App VoiceCipher 算法模拟加密（SHA-256 派生密钥 + iv 12B + AAD=seq 8B BE）"""
    iv = b"APPIV" + bytes(7)  # 固定 12B iv（5+7=12；App 实际随机）
    ct = AESGCM(key).encrypt(iv, plaintext, struct.pack(">Q", seq))
    return iv + ct


def test_e2ee_interop_app_encrypt_pc_decrypt():
    """App 端 VoiceCipher 加密帧 → PC RelayE2EE 可解（同一 passphrase 派生密钥）"""
    key = derive_key_from_passphrase(DEFAULT_E2EE_PASSPHRASE)
    pc_e2ee = RelayE2EE(key)
    plain = b"\x00\x01\x7f\x80\xff" * 40
    for seq in (0, 1, 42, 0xFFFFFFFF):
        enc = _app_style_encrypt(key, seq, plain)
        assert len(enc) == 12 + len(plain) + 16
        assert pc_e2ee.decrypt_audio(seq, 12345, enc) == plain
    # 换 seq 解不了（AAD 绑定 seq）
    enc = _app_style_encrypt(key, 7, plain)
    with pytest.raises(Exception):
        pc_e2ee.decrypt_audio(8, 12345, enc)


def test_e2ee_interop_pc_encrypt_app_decrypt():
    """PC RelayE2EE 加密帧 → App VoiceCipher 算法可解（反向互通）"""
    pc_e2ee = RelayE2EE(derive_key_from_passphrase(DEFAULT_E2EE_PASSPHRASE))
    key = derive_key_from_passphrase(DEFAULT_E2EE_PASSPHRASE)
    plain = b"pc-downlink-audio"
    enc = pc_e2ee.encrypt_audio(9, 999, plain)
    assert len(enc) == 12 + len(plain) + 16
    # App 侧 decrypt：iv=data[:12]，AAD=seq8B
    iv, ct = enc[:12], enc[12:]
    assert AESGCM(key).decrypt(iv, ct, struct.pack(">Q", 9)) == plain


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
