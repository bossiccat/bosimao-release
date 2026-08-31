'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  inspectLegacyPublishLock,
  parseMigrationOptions,
} = require('../lib/sidecar-runtime-migration-command');
const {
  diagnosticCode,
  ensureNativePointerHelper,
  main,
  requireNativePointerHelper,
} = require('../build-sidecar-external-bin');

test('build command module loads with the consumer probe wired once', () => {
  assert.equal(typeof diagnosticCode, 'function');
  assert.equal(typeof ensureNativePointerHelper, 'function');
});

test('migration command requires explicit flag and a backup directory', () => {
  assert.deepEqual(parseMigrationOptions([]), { migrate: false, backupDir: null });
  assert.throws(
    () => parseMigrationOptions(['--migrate-legacy-runtime']),
    /SIDECAR_RUNTIME_MIGRATION_BACKUP_REQUIRED/,
  );
  assert.throws(
    () => parseMigrationOptions(['--migrate-legacy-runtime', '--backup-dir', 'backup', '--verify-only']),
    /SIDECAR_RUNTIME_MIGRATION_MODE_CONFLICT/,
  );
  assert.deepEqual(
    parseMigrationOptions(['--migrate-legacy-runtime', '--backup-dir', 'C:\\runtime.backup']),
    { migrate: true, backupDir: 'C:\\runtime.backup' },
  );
});

test('legacy publish lock inspection is fail-closed unless the sibling lock path is absent', () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-runtime-lock-inspection-'));
  const runtimeDir = path.join(parent, 'jax-rtc-sidecar-runtime');
  fs.mkdirSync(runtimeDir);

  assert.deepEqual(inspectLegacyPublishLock(runtimeDir), { status: 'absent' });

  const lockDir = `${runtimeDir}.publish-lock`;
  fs.mkdirSync(lockDir);
  assert.deepEqual(inspectLegacyPublishLock(runtimeDir), { status: 'ambiguous' });

  fs.writeFileSync(path.join(lockDir, 'owner.json'), '{"token":"x","pid":1}');
  assert.deepEqual(inspectLegacyPublishLock(runtimeDir), { status: 'ambiguous' });
});

test('publisher requires a reproducible release pointer helper before it can publish on Windows', { skip: process.platform !== 'win32' }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-pointer-helper-build-'));
  const helperPath = path.join(root, 'target', 'release', 'sidecar-pointer-replace.exe');
  const invocations = [];

  const result = ensureNativePointerHelper({
    manifestPath: path.join(root, 'Cargo.toml'),
    helperPath,
    cargoPath: 'cargo',
    execute(command, args) {
      invocations.push({ command, args });
      fs.mkdirSync(path.dirname(helperPath), { recursive: true });
      fs.writeFileSync(helperPath, 'MZ');
      return { status: 0, error: null };
    },
  });

  assert.equal(result, helperPath);
  assert.deepEqual(invocations, [{
    command: 'cargo',
    args: ['build', '--release', '--manifest-path', path.join(root, 'Cargo.toml')],
  }]);
  assert.equal(fs.existsSync(helperPath), true);
});

test('verify-only requires an existing release pointer helper without invoking cargo', { skip: process.platform !== 'win32' }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-pointer-helper-verify-'));
  const helperPath = path.join(root, 'target', 'release', 'sidecar-pointer-replace.exe');
  const invocations = [];

  assert.throws(
    () => requireNativePointerHelper({
      helperPath,
      execute(command) { invocations.push(command); },
    }),
    /SIDECAR_POINTER_REPLACE_HELPER_MISSING/,
  );
  assert.deepEqual(invocations, []);

  fs.mkdirSync(path.dirname(helperPath), { recursive: true });
  fs.writeFileSync(helperPath, 'MZ');
  assert.equal(requireNativePointerHelper({ helperPath }), helperPath);
});

test('publisher fails closed when a release pointer helper build does not produce an executable', { skip: process.platform !== 'win32' }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-pointer-helper-build-fail-'));
  const helperPath = path.join(root, 'target', 'release', 'sidecar-pointer-replace.exe');

  assert.throws(
    () => ensureNativePointerHelper({
      manifestPath: path.join(root, 'Cargo.toml'),
      helperPath,
      execute: () => ({ status: 1, error: null }),
    }),
    /SIDECAR_POINTER_REPLACE_HELPER_BUILD_FAILED/,
  );
  assert.equal(fs.existsSync(helperPath), false);
});

test('migration command preserves its stable fail-closed diagnostic', () => {
  assert.equal(
    diagnosticCode({ code: 'SIDECAR_RUNTIME_MIGRATION_CONSUMERS_ACTIVE' }),
    'SIDECAR_RUNTIME_MIGRATION_CONSUMERS_ACTIVE',
  );
  assert.equal(
    diagnosticCode({ message: 'SIDECAR_RUNTIME_MIGRATION_LOCK_UNSAFE: phase=preflight' }),
    'SIDECAR_RUNTIME_MIGRATION_LOCK_UNSAFE',
  );
  assert.equal(
    diagnosticCode({ code: 'SIDECAR_POINTER_REPLACE_HELPER_BUILD_FAILED' }),
    'SIDECAR_POINTER_REPLACE_HELPER_BUILD_FAILED',
  );
  assert.equal(diagnosticCode(new Error('unexpected')), 'SIDECAR_PACKAGE_UNEXPECTED_FAILURE');
});

test('ordinary publish holds the shared coordination lease across the build', () => {
  const events = [];
  const helperDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ptr-'));
  const helperPath = path.join(helperDir, 'sidecar-pointer-replace.exe');
  fs.writeFileSync(helperPath, '');
  const originalWrite = process.stdout.write;
  process.stdout.write = () => true;
  try {
    main({
      args: [],
      helperPath,
      execute: () => ({ status: 0 }),
      packageConfig: () => ({ runtimeDir: 'C://jax//binaries//jax-rtc-sidecar-runtime', installedFile: 'jax-rtc-sidecar.exe' }),
      acquireRuntimeLease: (runtimeDir, options) => {
        events.push(`acquire:${options.operation}`);
        return () => events.push('release');
      },
      buildPackage: () => {
        events.push('build');
        return { external_bin: { build_input_file: 'input.exe', sha256: 'abc' } };
      },
      verifyPackage: () => {
        events.push('verify');
        return {};
      },
      trustGate: () => events.push('trust'),
    });
  } finally {
    process.stdout.write = originalWrite;
  }
  assert.deepEqual(events, ['acquire:publish', 'build', 'trust', 'release']);
});

test('the lease is released even when the build fails', () => {
  const events = [];
  const helperDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ptr-'));
  const helperPath = path.join(helperDir, 'sidecar-pointer-replace.exe');
  fs.writeFileSync(helperPath, '');
  assert.throws(
    () => main({
      args: [],
      helperPath,
      execute: () => ({ status: 0 }),
      packageConfig: () => ({ runtimeDir: 'C://jax//binaries//jax-rtc-sidecar-runtime' }),
      acquireRuntimeLease: (runtimeDir, options) => {
        events.push(`acquire:${options.operation}`);
        return () => events.push('release');
      },
      buildPackage: () => {
        throw new Error('SIDECAR_PACKAGE_BUILD_FAILED');
      },
    }),
    /SIDECAR_PACKAGE_BUILD_FAILED/,
  );
  assert.deepEqual(events, ['acquire:publish', 'release']);
});

test('migration acquires the same shared lease before any runtime mutation', () => {
  const events = [];
  const helperDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ptr-'));
  const helperPath = path.join(helperDir, 'sidecar-pointer-replace.exe');
  fs.writeFileSync(helperPath, '');
  const runtimeParent = fs.mkdtempSync(path.join(os.tmpdir(), 'migrate-'));
  assert.throws(() => main({
    args: ['--migrate-legacy-runtime', '--backup-dir', path.join(runtimeParent, 'backup')],
    helperPath,
    execute: () => ({ status: 0 }),
    packageConfig: () => ({
      runtimeDir: path.join(runtimeParent, 'jax-rtc-sidecar-runtime'),
      installedFile: 'jax-rtc-sidecar.exe',
    }),
    acquireRuntimeLease: (runtimeDir, options) => {
      events.push(`acquire:${options.operation}`);
      return () => events.push('release');
    },
  }));
  assert.deepEqual(events, ['acquire:migration', 'release']);
  assert.equal(fs.existsSync(path.join(runtimeParent, 'backup')), false);
});
