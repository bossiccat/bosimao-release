'use strict';

const fs = require('node:fs');
const path = require('node:path');

class MigrationError extends Error {
  constructor(code, state, cause) {
    super(`${code}: phase=${state.phase}${cause ? ` cause=${cause.message}` : ''}`);
    this.name = 'MigrationError';
    this.code = code;
    this.state = state;
    this.cause = cause;
  }
}

function fail(code, state = { phase: 'preflight', moved: false, backupPreserved: false, failClosed: true }) {
  throw new MigrationError(code, state);
}

function assertPlainDirectory(target, code, state) {
  let metadata;
  try {
    metadata = fs.lstatSync(target);
  } catch {
    fail(code, state);
  }
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) fail(code, state);
}

function isReparsePoint(metadata) {
  return metadata.isSymbolicLink() || (process.platform === 'win32' && (metadata.mode & fs.constants.S_IFMT) === 0);
}

function walkPlainTree(root, state) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    const metadata = fs.lstatSync(target);
    if (isReparsePoint(metadata)) fail('SIDECAR_RUNTIME_MIGRATION_REPARSE_POINT', state);
    if (metadata.isDirectory()) {
      walkPlainTree(target, state);
    } else if (!metadata.isFile()) {
      fail('SIDECAR_RUNTIME_MIGRATION_REPARSE_POINT', state);
    }
  }
}

function assertLegacyFlatRuntime(runtimeDir, state) {
  assertPlainDirectory(runtimeDir, 'SIDECAR_RUNTIME_MIGRATION_RUNTIME_MISSING', state);
  const legacyExecutable = path.join(runtimeDir, 'jax-rtc-sidecar.exe');
  const stableMarkers = ['current.json', 'generations', 'leases', 'publish.lock', 'reader-gc.lock', 'staging'];
  if (!fs.existsSync(legacyExecutable) || stableMarkers.some((entry) => fs.existsSync(path.join(runtimeDir, entry)))) {
    fail('SIDECAR_RUNTIME_MIGRATION_NOT_LEGACY_FLAT', state);
  }
  walkPlainTree(runtimeDir, state);
}

function assertBackupPath(runtimeDir, backupDir, state) {
  const runtimeAbsolute = path.resolve(runtimeDir);
  const parent = path.dirname(runtimeAbsolute);
  if (typeof backupDir !== 'string' || path.dirname(path.resolve(backupDir)) !== parent || path.resolve(backupDir) === runtimeAbsolute) {
    fail('SIDECAR_RUNTIME_MIGRATION_BACKUP_PATH_INVALID', state);
  }
  assertPlainDirectory(parent, 'SIDECAR_RUNTIME_MIGRATION_BACKUP_PATH_INVALID', state);
  if (fs.realpathSync(parent) !== parent) fail('SIDECAR_RUNTIME_MIGRATION_BACKUP_PATH_INVALID', state);
  if (fs.existsSync(backupDir)) fail('SIDECAR_RUNTIME_MIGRATION_BACKUP_EXISTS', state);
}

function assertOffline(inspectConsumers, inspectLockOwner, runtimeDir, state) {
  const consumers = inspectConsumers(runtimeDir);
  if (!Array.isArray(consumers)) fail('SIDECAR_RUNTIME_MIGRATION_CONSUMER_PROBE_INVALID', state);
  if (consumers.length > 0) fail('SIDECAR_RUNTIME_MIGRATION_CONSUMERS_ACTIVE', state);

  const owner = inspectLockOwner(runtimeDir);
  if (!owner || owner.status !== 'absent') fail('SIDECAR_RUNTIME_MIGRATION_LOCK_UNSAFE', state);
}

function failClosed(runtimeDir, state) {
  if (fs.existsSync(runtimeDir)) {
    try {
      fs.rmSync(path.join(runtimeDir, 'current.json'), { force: true });
    } catch {
      state.failClosed = false;
    }
  }
}

function migrateLegacyRuntime(input) {
  if (!input || typeof input !== 'object') fail('SIDECAR_RUNTIME_MIGRATION_INPUT_INVALID');
  const {
    runtimeDir,
    backupDir,
    acquireMigrationLock,
    inspectConsumers,
    inspectLockOwner,
    publish,
    verify,
  } = input;
  if (typeof runtimeDir !== 'string' || typeof acquireMigrationLock !== 'function'
    || typeof inspectConsumers !== 'function' || typeof inspectLockOwner !== 'function'
    || typeof publish !== 'function' || typeof verify !== 'function') {
    fail('SIDECAR_RUNTIME_MIGRATION_INPUT_INVALID');
  }

  const state = { phase: 'preflight', moved: false, backupPreserved: false, failClosed: true };
  const releaseMigrationLock = acquireMigrationLock(path.dirname(path.resolve(runtimeDir)));
  if (typeof releaseMigrationLock !== 'function') fail('SIDECAR_RUNTIME_MIGRATION_INPUT_INVALID');

  let result;
  let failure;
  try {
    assertLegacyFlatRuntime(runtimeDir, state);
    assertBackupPath(runtimeDir, backupDir, state);
    assertOffline(inspectConsumers, inspectLockOwner, runtimeDir, state);

    state.phase = 'move-legacy';
    fs.renameSync(runtimeDir, backupDir);
    state.moved = true;
    state.backupPreserved = true;

    state.phase = 'create-stable-root';
    fs.mkdirSync(runtimeDir);
    state.phase = 'publish';
    publish(runtimeDir);
    state.phase = 'verify';
    verify(runtimeDir);
    state.phase = 'complete';
    result = { runtimeDir, backupDir, ...state };
  } catch (cause) {
    if (state.moved) failClosed(runtimeDir, state);
    failure = cause instanceof MigrationError
      ? cause
      : new MigrationError('SIDECAR_RUNTIME_MIGRATION_POSTMOVE_FAILED', state, cause);
  }

  try {
    releaseMigrationLock();
  } catch (cause) {
    throw new MigrationError('SIDECAR_RUNTIME_MIGRATION_LOCK_RELEASE_FAILED', state, failure || cause);
  }
  if (failure) throw failure;
  return result;
}

module.exports = {
  MigrationError,
  migrateLegacyRuntime,
};
