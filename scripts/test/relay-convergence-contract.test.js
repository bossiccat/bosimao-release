const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..', '..');
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8');
const services = read('scripts/jax-services.ps1');
const watchdog = read('scripts/jax-watchdog.ps1');

test('relay startup converges duplicate top-level instances before spawning', () => {
  assert.match(services, /if \(\$existing\.Count -eq 1\)/);
  assert.match(services, /if \(\$existing\.Count -gt 1\)/);
  assert.match(services, /Stop-AllRelay/);
  assert.match(services, /Get-RelayTopLevel/);
  assert.match(services, /Get-RelayProcesses/);
  assert.match(services, /return \$false/);
});

test('watchdog accepts exactly one complete relay tree only', () => {
  assert.match(watchdog, /Test-RelayProcessTree/);
  assert.match(watchdog, /return \(-not \(Test-RelayDeadLoop\)\)/);
});

test('all service entry points ship their shared library dependency', () => {
  for (const file of ['scripts/lib-common.ps1', 'scripts/jax-services.ps1', 'scripts/jax-watchdog.ps1', 'scripts/start-all.ps1']) {
    assert.ok(fs.existsSync(path.join(root, file)), `${file} must be present in the delivery tree`);
  }
});
