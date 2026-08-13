'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const { spawn } = require('child_process');
const { createExitArbiter } = require('../exit-protocol');
const { startPollingRuntime } = require('../rtc-startup');

const SIDECAR = path.resolve(__dirname, '..');
const ELECTRON = path.join(SIDECAR, 'node_modules', 'electron', 'dist', 'electron.exe');
const FIXTURE = path.join(__dirname, 'exit-protocol-fixture.js');

function runFixture(kind) {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.NODE_OPTIONS;
  return new Promise((resolve, reject) => {
    const child = spawn(ELECTRON, ['--no-sandbox', FIXTURE, kind], {
      cwd: SIDECAR,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`exit fixture timeout: ${stderr}`));
    }, 20000);
    child.on('exit', (code) => {
      clearTimeout(timer);
      resolve(code);
    });
  });
}

test('renderer controlled request exits Electron main with code 0', async () => {
  assert.equal(await runFixture('controlled'), 0);
});

test('renderer fatal request exits Electron main with code 2', async () => {
  assert.equal(await runFixture('fatal'), 2);
});

test('invalid payload is fatal and repeated requests preserve the first verdict', () => {
  const exits = [];
  const arbiter = createExitArbiter((code) => exits.push(code));
  assert.equal(arbiter.decide({ kind: 'controlled', extra: true }), 2);
  assert.equal(arbiter.decide({ kind: 'controlled' }), 2);
  assert.deepEqual(exits, [2]);

  const controlledExits = [];
  const controlled = createExitArbiter((code) => controlledExits.push(code));
  assert.equal(controlled.decide({ kind: 'controlled' }), 0);
  assert.equal(controlled.decide({ kind: 'fatal' }), 0);
  assert.deepEqual(controlledExits, [0]);
});

test('runSidecar throw requests fatal and never starts polling timers', () => {
  const calls = [];
  const started = startPollingRuntime({
    runSidecar: () => { throw new Error('fixture failure'); },
    pollAndJoin: () => calls.push('poll'),
    scheduleInterval: () => calls.push('interval'),
    scheduleTimeout: () => calls.push('timeout'),
    requestFatal: () => calls.push('fatal'),
    logFatal: () => calls.push('log'),
  });
  assert.equal(started, false);
  assert.deepEqual(calls, ['log', 'fatal']);
});
