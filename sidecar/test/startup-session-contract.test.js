'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { parseArgList, validateStartup } = require('../config');
const { BridgeClient } = require('../bridge');
const { startPollingRuntime } = require('../rtc-startup');

const RTC_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'rtc.js'), 'utf8');
const PHONE_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'phone.js'), 'utf8');
const MAIN_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');

function validCredential() {
  return 's'.repeat(32);
}

test('production sidecar accepts no device and rejects device before startup', () => {
  const sidecar = parseArgList(['--role=sidecar']);
  assert.equal(sidecar.device, undefined);
  assert.equal(validateStartup(sidecar, { VOICE_SIDECAR_CREDENTIAL: validCredential() }), null);
  assert.equal(
    validateStartup(parseArgList(['--role=sidecar', '--device=android-1']), {
      VOICE_SIDECAR_CREDENTIAL: validCredential(),
    }),
    'SIDECAR_UNEXPECTED_DEVICE_ARG',
  );
});

test('role-scoped startup validation is fail-closed', () => {
  assert.equal(validateStartup(parseArgList(['--role=sidecar']), {}), 'SIDECAR_CREDENTIAL_MISSING');
  assert.equal(validateStartup(parseArgList(['--role=phone']), {}), 'PHONE_DEVICE_REQUIRED');
  assert.equal(validateStartup(parseArgList(['--role=phone', '--device=android-1']), {}), null);
  assert.equal(validateStartup(parseArgList(['--role=unknown']), {}), 'SIDECAR_INVALID_ARGS');
  assert.equal(validateStartup(parseArgList(['--role=sidecar', '--role=phone']), {}), 'SIDECAR_INVALID_ARGS');
});

test('production polling runtime remains resident beyond the hold window', () => {
  const scheduledTimeouts = [];
  const calls = [];
  const started = startPollingRuntime({
    role: 'sidecar',
    holdS: 1,
    runSidecar: () => calls.push('run'),
    pollAndJoin: () => calls.push('poll'),
    scheduleInterval: () => calls.push('interval'),
    scheduleTimeout: (fn, delay) => scheduledTimeouts.push({ fn, delay }),
    requestControlled: () => calls.push('controlled'),
    requestFatal: () => calls.push('fatal'),
    logFatal: () => calls.push('log'),
  });
  assert.equal(started, true);
  assert.doesNotMatch(RTC_SOURCE, /exitSidecar\(['"]hold_timeout['"]\)/);
  for (const timer of scheduledTimeouts.filter(({ delay }) => delay <= 1000)) timer.fn();
  assert.deepEqual(calls, ['run', 'interval', 'poll']);
  assert.equal(scheduledTimeouts.some(({ delay }) => delay === 1000), false);
});

test('phone role retains an explicit bounded hold lifecycle', () => {
  assert.match(PHONE_SOURCE, /ARGS\.holdS\s*\*\s*1000/);
  assert.match(PHONE_SOURCE, /exitPhone\(\)/);
});

test('Electron main validates before creating a hidden renderer and exits non-zero', () => {
  assert.match(MAIN_SOURCE, /validateStartup/);
  assert.match(MAIN_SOURCE, /createExitArbiter\(\(code\) => app\.exit\(code\)\)/);
  assert.ok(MAIN_SOURCE.indexOf('validateStartup') < MAIN_SOURCE.indexOf('new BrowserWindow'));
});

test('bridge has no startup hello and only connects for a complete active session', () => {
  const opened = [];
  const original = global.WebSocket;
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; opened.push(this); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; }
  }
  global.WebSocket = FakeWebSocket;
  try {
    const bridge = new BridgeClient('ws://127.0.0.1:19092', () => {}, () => {});
    assert.equal(opened.length, 0);
    assert.throws(() => bridge.startSession({ session_id: '', device_id: 'd', room_id: 'r', user_id: 'u', sdk_version: 'v' }));
    bridge.startSession({ session_id: 's1', device_id: 'd1', room_id: 'r1', user_id: 'u', sdk_version: 'v' });
    assert.equal(opened.length, 1);
    opened[0].readyState = FakeWebSocket.OPEN;
    opened[0].onopen();
    assert.deepEqual(opened[0].sent[0], {
      type: 'hello', role: 'sidecar', sdk_version: 'v', session_id: 's1', device_id: 'd1', room_id: 'r1', user_id: 'u',
    });
    bridge.clearSession();
    opened[0].onclose();
    assert.equal(opened.length, 1, 'no active session must not reconnect');
  } finally {
    global.WebSocket = original;
  }
});

test('a changed session replaces the socket and sends its hello as the first frame', () => {
  const opened = [];
  const original = global.WebSocket;
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    constructor() { this.readyState = FakeWebSocket.CONNECTING; this.sent = []; this.closed = false; opened.push(this); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.closed = true; this.readyState = 3; }
  }
  global.WebSocket = FakeWebSocket;
  const sessionA = { session_id: 'sA', device_id: 'dA', room_id: 'rA', user_id: 'u', sdk_version: 'v' };
  const sessionB = { session_id: 'sB', device_id: 'dB', room_id: 'rB', user_id: 'u', sdk_version: 'v' };
  try {
    const bridge = new BridgeClient('ws://127.0.0.1:19092', () => {}, () => {});
    bridge.startSession(sessionA);
    opened[0].readyState = FakeWebSocket.OPEN;
    opened[0].onopen();
    bridge.startSession(sessionB);
    assert.equal(opened[0].closed, true, 'old session socket must close');
    assert.equal(opened.length, 2, 'changed session must create a new socket');
    assert.equal(opened[0].sent.length, 1, 'session B hello must not use old socket');
    opened[1].readyState = FakeWebSocket.OPEN;
    opened[1].onopen();
    assert.deepEqual(opened[1].sent, [{ type: 'hello', role: 'sidecar', ...sessionB }]);
    opened[0].onclose();
    assert.equal(opened.length, 2, 'stale onclose must not create a competing socket');
    bridge.startSession(sessionB);
    assert.equal(opened.length, 2, 'same session must be idempotent');
  } finally {
    global.WebSocket = original;
  }
});

test('pending/sign flow checks room before bridge hello and signs the Android intent device', () => {
  assert.match(RTC_SOURCE, /device_id:\s*intent\.device_id/);
  assert.match(RTC_SOURCE, /cred\.room_id\s*!==\s*intent\.room_id/);
  assert.match(RTC_SOURCE, /bridge\.startSession\(/);
  assert.ok(RTC_SOURCE.indexOf('cred.room_id !== intent.room_id') < RTC_SOURCE.indexOf('bridge.startSession('));
  assert.doesNotMatch(RTC_SOURCE, /bridge\.start\(/);
});
