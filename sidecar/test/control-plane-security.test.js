'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const security = require('../security');

const RTC_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'rtc.js'), 'utf8');
const CONFIG_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'config.js'), 'utf8');

test('session requests use an independent Bearer credential and fresh high-entropy nonce', () => {
  const first = security.controlPlaneHeaders({ credential: 'test-sidecar-credential' });
  const second = security.controlPlaneHeaders({ credential: 'test-sidecar-credential' });
  assert.equal(first.Authorization, 'Bearer test-sidecar-credential');
  assert.match(first['X-Request-Nonce'], /^[0-9a-f]{64}$/);
  assert.match(second['X-Request-Nonce'], /^[0-9a-f]{64}$/);
  assert.notEqual(first['X-Request-Nonce'], second['X-Request-Nonce']);
});

test('rtc session and pending requests attach security headers', () => {
  assert.match(RTC_SOURCE, /controlPlaneHeaders\(/);
  assert.match(RTC_SOURCE, /\/api\/v1\/voice\/session\/pending/);
  const securedRequests = RTC_SOURCE.match(/headers:\s*controlPlaneHeaders\(/g) || [];
  assert.equal(securedRequests.length, 2);
});

test('production sidecar never carries or derives a TRTC SecretKey', () => {
  assert.doesNotMatch(RTC_SOURCE, /secretKeyFallback|genUserSig\(/);
  assert.doesNotMatch(CONFIG_SOURCE, /secretKeyFallback|TRTC_SECRETKEY/);
  assert.match(CONFIG_SOURCE, /process\.env\.VOICE_SIDECAR_CREDENTIAL/);
  assert.doesNotMatch(CONFIG_SOURCE, /sidecarCredential:\s*env\.VOICE_SIDECAR_CREDENTIAL/);
});

test('security helper rejects missing production credential instead of falling back', () => {
  assert.throws(() => security.controlPlaneHeaders({}), /sidecar credential unavailable/);
});
