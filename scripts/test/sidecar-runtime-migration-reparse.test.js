'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { migrateLegacyRuntime } = require('../lib/sidecar-runtime-migration');

// 真机 reparse 证据：POSIX symlink 测试无法证明 Windows junction / mount point
// 的拒绝行为，这里必须在本机创建真实 junction 并验证迁移被阻断。
function createJunction(linkPath, targetPath) {
  for (const candidate of [linkPath, targetPath]) {
    assert.equal(candidate.includes("'"), false, 'fixture paths must not contain quotes');
  }
  const result = spawnSync(
    'powershell.exe',
    [
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `New-Item -ItemType Junction -Path '${linkPath}' -Target '${targetPath}' | Out-Null`,
    ],
    { encoding: 'utf8', windowsHide: true },
  );
  return result.status === 0;
}

const createdRoots = [];

test.after(() => {
  // junction 不能靠递归删除安全清理，必须显式删除链接本身再删树，
  // 否则临时目录会残留 reparse point，污染后续扫描。
  for (const root of createdRoots) {
    if (!fs.existsSync(root)) continue;
    spawnSync(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command', `Remove-Item -LiteralPath '${root}' -Recurse -Force`],
      { encoding: 'utf8', windowsHide: true },
    );
  }
});

function fixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-runtime-reparse-'));
  createdRoots.push(parent);
  const runtimeDir = path.join(parent, 'jax-rtc-sidecar-runtime');
  const backupDir = path.join(parent, 'jax-rtc-sidecar-runtime.legacy-backup');
  fs.mkdirSync(runtimeDir);
  fs.writeFileSync(path.join(runtimeDir, 'jax-rtc-sidecar.exe'), 'legacy-runtime');
  fs.writeFileSync(path.join(runtimeDir, 'resources.pak'), 'legacy-resources');
  return { parent, runtimeDir, backupDir };
}

function migration(input, overrides = {}) {
  return migrateLegacyRuntime({
    ...input,
    acquireMigrationLock: () => () => {},
    inspectConsumers: () => [],
    inspectLockOwner: () => ({ status: 'absent' }),
    publish: () => {
      fs.mkdirSync(path.join(input.runtimeDir, 'generations', `g-${'a'.repeat(64)}`), { recursive: true });
      fs.writeFileSync(path.join(input.runtimeDir, 'current.json'), '{}');
    },
    verify: () => {},
    ...overrides,
  });
}

test('rejects a legacy tree containing a real Windows junction', {
  skip: process.platform !== 'win32' ? 'windows-only reparse evidence' : false,
}, () => {
  const input = fixture();
  const outside = path.join(input.parent, 'outside-payload');
  fs.mkdirSync(outside);
  const link = path.join(input.runtimeDir, 'linked-payload');
  assert.equal(createJunction(link, outside), true, 'junction fixture must be created');

  const metadata = fs.lstatSync(link);
  assert.equal(metadata.isSymbolicLink(), true, 'junction fixture must be a reparse point');

  assert.throws(() => migration(input), /SIDECAR_RUNTIME_MIGRATION_REPARSE_POINT/);
  assert.equal(fs.existsSync(input.backupDir), false);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), true);
});

test('rejects a junction nested below the legacy root', {
  skip: process.platform !== 'win32' ? 'windows-only reparse evidence' : false,
}, () => {
  const input = fixture();
  const nested = path.join(input.runtimeDir, 'resources');
  fs.mkdirSync(nested);
  const outside = path.join(input.parent, 'outside-nested');
  fs.mkdirSync(outside);
  const link = path.join(nested, 'app');
  assert.equal(createJunction(link, outside), true, 'nested junction fixture must be created');

  assert.throws(() => migration(input), /SIDECAR_RUNTIME_MIGRATION_REPARSE_POINT/);
  assert.equal(fs.existsSync(input.backupDir), false);
});
