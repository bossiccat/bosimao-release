'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { migrateLegacyRuntime } = require('../lib/sidecar-runtime-migration');

function fixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-runtime-migration-'));
  const runtimeDir = path.join(parent, 'jax-rtc-sidecar-runtime');
  const backupDir = path.join(parent, 'jax-rtc-sidecar-runtime.legacy-backup');
  fs.mkdirSync(runtimeDir);
  fs.writeFileSync(path.join(runtimeDir, 'jax-rtc-sidecar.exe'), 'legacy-runtime');
  fs.writeFileSync(path.join(runtimeDir, 'resources.pak'), 'legacy-resources');
  return { parent, runtimeDir, backupDir };
}

function migration(input, overrides = {}) {
  const calls = [];
  const result = migrateLegacyRuntime({
    ...input,
    acquireMigrationLock: () => () => {},
    inspectConsumers: () => [],
    inspectLockOwner: () => ({ status: 'absent' }),
    publish: () => {
      calls.push('publish');
      fs.mkdirSync(path.join(input.runtimeDir, 'generations', 'g-' + 'a'.repeat(64)), { recursive: true });
      fs.writeFileSync(path.join(input.runtimeDir, 'current.json'), '{}');
    },
    verify: () => calls.push('verify'),
    ...overrides,
  });
  return { result, calls };
}

function assertBackupHasOriginal(input) {
  assert.equal(fs.readFileSync(path.join(input.backupDir, 'jax-rtc-sidecar.exe'), 'utf8'), 'legacy-runtime');
  assert.equal(fs.readFileSync(path.join(input.backupDir, 'resources.pak'), 'utf8'), 'legacy-resources');
}

test('rejects migration when a sidecar consumer is still active', () => {
  const input = fixture();

  assert.throws(
    () => migration(input, { inspectConsumers: () => [{ pid: 73, executable: 'jax-pet.exe' }] }),
    /SIDECAR_RUNTIME_MIGRATION_CONSUMERS_ACTIVE/,
  );
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), true);
  assert.equal(fs.existsSync(input.backupDir), false);
});

test('rejects an ambiguous or live legacy publish lock', () => {
  const input = fixture();

  for (const status of ['live', 'ambiguous']) {
    assert.throws(
      () => migration(input, { inspectLockOwner: () => ({ status }) }),
      /SIDECAR_RUNTIME_MIGRATION_LOCK_UNSAFE/,
    );
    assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), true);
  }
});

test('rejects a backup path outside the runtime parent or an existing backup', () => {
  const input = fixture();

  assert.throws(
    () => migration({ ...input, backupDir: path.join(os.tmpdir(), 'outside-runtime-backup') }),
    /SIDECAR_RUNTIME_MIGRATION_BACKUP_PATH_INVALID/,
  );
  fs.mkdirSync(input.backupDir);
  assert.throws(
    () => migration(input),
    /SIDECAR_RUNTIME_MIGRATION_BACKUP_EXISTS/,
  );
});

test('rejects a runtime that is already stable-root or does not look legacy-flat', () => {
  const input = fixture();
  fs.rmSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe'));
  fs.mkdirSync(path.join(input.runtimeDir, 'generations'));
  fs.writeFileSync(path.join(input.runtimeDir, 'current.json'), '{}');

  assert.throws(
    () => migration(input),
    /SIDECAR_RUNTIME_MIGRATION_NOT_LEGACY_FLAT/,
  );
});

// Windows 侧的真机 junction 证据见 sidecar-runtime-migration-reparse.test.js，
// 该用例在 Windows 上以真实 junction 驱动同一条拒绝路径。
test('rejects a legacy tree containing a symlink before it moves any evidence', {
  skip: process.platform === 'win32' ? 'covered by the real-junction reparse suite on Windows' : false,
}, () => {
  const input = fixture();
  fs.symlinkSync(path.join(input.parent, 'outside-payload'), path.join(input.runtimeDir, 'linked-payload'));

  assert.throws(
    () => migration(input),
    /SIDECAR_RUNTIME_MIGRATION_REPARSE_POINT/,
  );
  assert.equal(fs.existsSync(input.backupDir), false);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), true);
});

test('holds the migration lock across final probes, publish and verify', () => {
  const input = fixture();
  const calls = [];
  let locked = false;

  migration(input, {
    acquireMigrationLock: () => {
      locked = true;
      calls.push('lock');
      return () => {
        locked = false;
        calls.push('unlock');
      };
    },
    inspectConsumers: () => {
      assert.equal(locked, true);
      calls.push('consumers');
      return [];
    },
    inspectLockOwner: () => {
      assert.equal(locked, true);
      calls.push('owner');
      return { status: 'absent' };
    },
    publish: () => {
      assert.equal(locked, true);
      calls.push('publish');
    },
    verify: () => {
      assert.equal(locked, true);
      calls.push('verify');
    },
  });

  assert.deepEqual(calls, ['lock', 'consumers', 'owner', 'publish', 'verify', 'unlock']);
  assert.equal(locked, false);
});

test('moves the full legacy runtime to an explicit sibling backup before publish then verify', () => {
  const input = fixture();
  const { result, calls } = migration(input);

  assert.deepEqual(calls, ['publish', 'verify']);
  assert.equal(result.backupDir, input.backupDir);
  assertBackupHasOriginal(input);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), false);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'generations')), true);
});

test('retains the backup and removes a partial pointer when publish fails', () => {
  const input = fixture();

  assert.throws(
    () => migration(input, {
      publish: () => {
        fs.writeFileSync(path.join(input.runtimeDir, 'current.json'), '{"generation":"partial"}');
        throw new Error('publisher failed');
      },
    }),
    /publisher failed/,
  );
  assertBackupHasOriginal(input);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'current.json')), false);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), false);
});

test('retains the backup and removes a published pointer when verify fails', () => {
  const input = fixture();

  assert.throws(
    () => migration(input, { verify: () => { throw new Error('verification failed'); } }),
    /verification failed/,
  );
  assertBackupHasOriginal(input);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'current.json')), false);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'jax-rtc-sidecar.exe')), false);
});

test('releases the migration lock when a post-move phase fails', () => {
  const input = fixture();
  const calls = [];

  assert.throws(
    () => migration(input, {
      acquireMigrationLock: () => {
        calls.push('lock');
        return () => calls.push('unlock');
      },
      publish: () => { throw new Error('publisher failed'); },
    }),
    /publisher failed/,
  );
  assert.deepEqual(calls, ['lock', 'unlock']);
});

test('reports lock release failure without hiding that migration moved the runtime', () => {
  const input = fixture();

  assert.throws(
    () => migration(input, {
      acquireMigrationLock: () => () => { throw new Error('unlock failed'); },
    }),
    (error) => error.code === 'SIDECAR_RUNTIME_MIGRATION_LOCK_RELEASE_FAILED'
      && error.state.moved === true
      && error.state.backupPreserved === true
      && error.state.phase === 'complete',
  );
  assertBackupHasOriginal(input);
  assert.equal(fs.existsSync(path.join(input.runtimeDir, 'current.json')), true);
});
