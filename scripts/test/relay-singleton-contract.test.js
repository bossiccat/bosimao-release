'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const scriptsDir = path.resolve(__dirname, '..');
const servicesPath = path.join(scriptsDir, 'jax-services.ps1');
const watchdogPath = path.join(scriptsDir, 'jax-watchdog.ps1');
const commonPath = path.join(scriptsDir, 'lib-common.ps1');

function source(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

test('relay service converges duplicate top-level instances before spawning', () => {
  const services = source(servicesPath);

  assert.match(services, /if \(\$existing\.Count -eq 1 -and \(Test-RelayProcessTree\)\)\s*\{[\s\S]*?return \$true/s);
  assert.match(services, /if \(\$existing\.Count -gt 1\)\s*\{[\s\S]*?if \(-not \(Stop-AllRelay\)\)[\s\S]*?return \$false/s);
  assert.match(services, /@\(Get-RelayProcesses\)\.Count -ne 0[\s\S]*?return \$false/s);
  assert.match(services, /if \(\$Name -eq "relay"\)\s*\{[\s\S]*?if \(-not \(Stop-AllRelay\)\)[\s\S]*?return \$false/s);
  assert.doesNotMatch(services, /if \(\$existing\.Count -gt 0\)\s*\{[\s\S]{0,500}?return \$true/s);
});

test('relay health requires exactly one complete project relay process tree', () => {
  const services = source(servicesPath);
  const watchdog = source(watchdogPath);
  const common = source(commonPath);

  assert.match(common, /CommandLine\s+-match\s+"relay_client"/);
  assert.match(services, /function\s+Test-RelayProcessTree\b/i);
  assert.match(services, /\$topLevel\.Count -ne 1\) \{ return \$false \}/);
  assert.match(watchdog, /function\s+Test-RelayProcessTree\b/i);
  assert.match(watchdog, /\$topLevel\.Count -ne 1\) \{ return \$false \}/);
  assert.match(watchdog, /function\s+Test-RelayAlive\s*\{[\s\S]*?if \(-not \(Test-RelayProcessTree\)\) \{ return \$false \}[\s\S]*?return \(-not \(Test-RelayDeadLoop\)\)/s);
  assert.match(watchdog, /"relay"\s*\{\s*Test-RelayAlive\s*\}/);
  assert.doesNotMatch(watchdog, /"relay"\s*\{\s*@\(Get-RelayProcesses\)\.Count\s*-gt\s*0\s*\}/);
});

test('relay process discovery remains scoped to relay_client command lines', () => {
  const common = source(commonPath);

  assert.match(common, /Name='python\.exe' OR Name='pythonw\.exe'/);
  assert.match(common, /CommandLine\s+-match\s+"relay_client"/);
});
