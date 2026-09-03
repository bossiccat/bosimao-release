'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const watchdogPath = path.resolve(__dirname, '..', 'jax-watchdog.ps1');

function watchdogSource() {
  return fs.readFileSync(watchdogPath, 'utf8');
}

test('jax watchdog delegates desktop sidecar ownership to Tauri', () => {
  const source = watchdogSource();

  assert.doesNotMatch(source, /function\s+Start-Sidecar\b/i);
  assert.doesNotMatch(source, /function\s+Test-Sidecar(?:Healthy|Connected)\b/i);
  assert.doesNotMatch(source, /node_modules\\electron\\dist\\electron\.exe/i);
  assert.doesNotMatch(source, /--role=sidecar/i);
  assert.doesNotMatch(source, /foreach \(\$svc in @\([^\n]*"sidecar"/i);
  const executable = source
    .split(/\r?\n/)
    .filter((line) => !/^\s*#/.test(line))
    .join('\n');
  assert.doesNotMatch(executable, /\bsidecar\b/iu);
});

test('jax watchdog records restart attempts before invoking a service', () => {
  const source = watchdogSource();
  const record = source.indexOf('Record-Restart $svc');
  const invoke = source.indexOf('$out = & $SvcScript start $svc');
  assert.ok(record >= 0, 'restart attempt must be recorded');
  assert.ok(invoke > record, 'recording must happen before spawn');
});
