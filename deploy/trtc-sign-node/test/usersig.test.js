// usersig 独立验签测试（假 SecretKey，禁止真实密钥）
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { genUserSig } = require('../usersig');
const { parseUserSig } = require('./verify');

const SDK = 1600155678;
const SK = 'fake-secret-key-for-test-only-0123456789';

test('userSig 可独立验签且字段正确', () => {
  const sig = genUserSig(SDK, SK, 'test-dev-1', 600);
  const p = parseUserSig(sig, SDK, SK);
  assert.equal(p.sigValid, true, 'HMAC 签名必须通过独立验签');
  assert.equal(p.appIdMatch, true);
  assert.equal(p.expireOk, true, 'expire 必须 ≤600');
  assert.equal(p.identifier, 'test-dev-1', 'identifier 必须 = device_id');
});

test('userSig 确定性（同输入同输出）', () => {
  const a = genUserSig(SDK, SK, 'test-dev-2', 600);
  const b = genUserSig(SDK, SK, 'test-dev-2', 600);
  assert.equal(a, b);
});

test('错密钥验签必须失败', () => {
  const sig = genUserSig(SDK, SK, 'test-dev-3', 600);
  const p = parseUserSig(sig, SDK, 'wrong-key-000');
  assert.equal(p.sigValid, false);
});

test('篡改 userSig 必须失败', () => {
  const sig = genUserSig(SDK, SK, 'test-dev-4', 600);
  const tampered = sig.slice(0, -5) + 'AAAAA';
  let rejected = false;
  try {
    const p = parseUserSig(tampered, SDK, SK);
    rejected = !p.sigValid;
  } catch (_e) {
    rejected = true; // zlib/CRC 校验失败也视为拒绝篡改
  }
  assert.equal(rejected, true);
});

test('不同 device 的 userSig identifier 互不相同', () => {
  const a = parseUserSig(genUserSig(SDK, SK, 'dev-a', 600), SDK, SK);
  const b = parseUserSig(genUserSig(SDK, SK, 'dev-b', 600), SDK, SK);
  assert.equal(a.identifier, 'dev-a');
  assert.equal(b.identifier, 'dev-b');
  assert.notEqual(a.identifier, b.identifier);
});
