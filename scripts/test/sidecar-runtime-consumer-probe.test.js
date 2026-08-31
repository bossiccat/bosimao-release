'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { inspectConsumers } = require('../lib/sidecar-runtime-consumer-probe');

const runtimeDir = 'C:\\Program Files\\Jax\\jax-rtc-sidecar-runtime';
const generation = runtimeDir + '\\generations\\g-abc';

function snapshot(overrides = {}) {
  return {
    processes: [
      { pid: 10, parentProcessId: 1, name: 'jax-pet.exe', executablePath: 'C:\\Program Files\\Jax\\jax-pet.exe', commandLine: `"C:\\Program Files\\Jax\\jax-pet.exe" --sidecar-runtime "${runtimeDir}"` },
      { pid: 20, parentProcessId: 10, name: 'jax-rtc-sidecar.exe', executablePath: generation + '\\jax-rtc-sidecar.exe', commandLine: `"${generation}\\jax-rtc-sidecar.exe" --role=sidecar` },
      { pid: 21, parentProcessId: 20, name: 'electron.exe', executablePath: generation + '\\electron.exe', commandLine: `"${generation}\\electron.exe" "${runtimeDir}\\resources\\app\\main.js" --role=sidecar` },
    ],
    ports: [{ port: 19092, owningPid: 20 }],
    ...overrides,
  };
}

const identityRealpath = (value) => value;
const probeOptions = (runner) => ({ platform: 'win32', runner, realpath: identityRealpath });

test('fails closed when a Windows process snapshot is empty', () => {
  const result = inspectConsumers(runtimeDir, probeOptions(() => ({ processes: [], ports: [] })));
  assert.deepEqual(result, [{ diagnostic: 'SIDECAR_RUNTIME_MIGRATION_CONSUMER_PROBE_PROCESS_INVENTORY_EMPTY' }]);
});

test('attributes jax-pet, sidecar and descendants with executable and command-line evidence', () => {
  const consumers = inspectConsumers(runtimeDir, probeOptions(() => snapshot()));
  assert.deepEqual(consumers.map((p) => p.pid), [10, 20, 21]);
  assert.equal(consumers.every((p) => p.evidence && p.evidence.length > 0), true);
});

test('attributes a legacy-flat sidecar launched with the production fixed argv before migration', () => {
  const legacySidecar = {
    pid: 30,
    parentProcessId: 1,
    name: 'jax-rtc-sidecar.exe',
    executablePath: `${runtimeDir}\\jax-rtc-sidecar.exe`,
    commandLine: `"${runtimeDir}\\jax-rtc-sidecar.exe" --role=sidecar`,
  };
  const consumers = inspectConsumers(runtimeDir, probeOptions(() => ({ processes: [legacySidecar], ports: [] })));
  assert.deepEqual(consumers.map((p) => p.pid), [30]);
  assert.match(consumers[0].evidence.join(','), /process-identity/);
});

test('attributes an immutable-generation sidecar launched with the production fixed argv', () => {
  const sidecar = {
    pid: 31,
    parentProcessId: 1,
    name: 'jax-rtc-sidecar.exe',
    executablePath: `${generation}\\jax-rtc-sidecar.exe`,
    commandLine: `"${generation}\\jax-rtc-sidecar.exe" --role=sidecar`,
  };
  const consumers = inspectConsumers(runtimeDir, probeOptions(() => ({ processes: [sidecar], ports: [] })));
  assert.deepEqual(consumers.map((p) => p.pid), [31]);
});

test('does not attribute a sidecar executable without the production role argument', () => {
  const s = snapshot({ processes: [{ ...snapshot().processes[1], commandLine: `"${generation}\\jax-rtc-sidecar.exe"` }] });
  const consumers = inspectConsumers(runtimeDir, probeOptions(() => s));
  assert.equal(consumers.some((p) => p.pid === 20), false);
});

test('fails closed when runner throws or returns malformed output', () => {
  for (const runner of [() => { throw new Error('access denied'); }, () => ({ processes: [{ pid: 'bad' }] })]) {
    const result = inspectConsumers(runtimeDir, probeOptions(runner));
    assert.equal(result.length, 1);
    assert.match(result[0].diagnostic, /^SIDECAR_RUNTIME_MIGRATION_CONSUMER_PROBE_/);
  }
});

test('does not attribute a port owner without process evidence', () => {
  const unrelated = {
    pid: 70,
    parentProcessId: 1,
    name: 'unrelated.exe',
    executablePath: 'C:\\Windows\\System32\\unrelated.exe',
    commandLine: '"C:\\Windows\\System32\\unrelated.exe"',
  };
  const result = inspectConsumers(runtimeDir, {
    platform: 'win32',
    runner: () => ({ processes: [unrelated], ports: [{ port: 19092, owningPid: 77 }] }),
    realpath: identityRealpath,
  });
  assert.deepEqual(result, []);
});

test('fails closed for a consumer executable outside the selected runtime generation', () => {
  const result = inspectConsumers(runtimeDir, probeOptions(() => snapshot({
    processes: [{ ...snapshot().processes[1], executablePath: 'C:\\\\Other\\\\jax-rtc-sidecar.exe', commandLine: '"C:\\\\Other\\\\jax-rtc-sidecar.exe" --runtime-dir "' + runtimeDir + '"' }],
  })));
  assert.equal(result.length, 1);
  assert.match(result[0].diagnostic, /^SIDECAR_RUNTIME_MIGRATION_CONSUMER_PROBE_/);
});

test('fails closed when JSON output is dirty or the runner exits nonzero', () => {
  for (const runner of [
    () => ({ status: 1, stdout: JSON.stringify({ processes: [], ports: [] }) }),
    () => ({ status: 0, stdout: 'warning\\n{"processes":[],"ports":[]}' }),
  ]) {
    const result = inspectConsumers(runtimeDir, { platform: 'win32', run: runner, realpath: identityRealpath });
    assert.equal(result.length, 1);
    assert.match(result[0].diagnostic, /^SIDECAR_RUNTIME_MIGRATION_CONSUMER_PROBE_/);
  }
});
