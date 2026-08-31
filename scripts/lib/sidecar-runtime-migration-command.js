'use strict';

const fs = require('node:fs');
const path = require('node:path');

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

function parseMigrationOptions(args) {
  const migrate = args.includes('--migrate-legacy-runtime');
  const backupIndex = args.indexOf('--backup-dir');
  const backupDir = backupIndex === -1 ? null : args[backupIndex + 1] || null;
  if (!migrate && backupIndex !== -1) fail('SIDECAR_RUNTIME_MIGRATION_MODE_REQUIRED');
  if (migrate && !backupDir) fail('SIDECAR_RUNTIME_MIGRATION_BACKUP_REQUIRED');
  if (migrate && args.includes('--verify-only')) fail('SIDECAR_RUNTIME_MIGRATION_MODE_CONFLICT');
  return { migrate, backupDir };
}

function inspectLegacyPublishLock(runtimeDir) {
  const lockPath = `${runtimeDir}.publish-lock`;
  return fs.existsSync(lockPath) ? { status: 'ambiguous' } : { status: 'absent' };
}

function acquireMigrationLock(runtimeParent) {
  const lockPath = path.join(runtimeParent, '.jax-rtc-sidecar-runtime.migration-lock');
  try {
    fs.mkdirSync(lockPath);
  } catch (error) {
    if (error && error.code === 'EEXIST') fail('SIDECAR_RUNTIME_MIGRATION_LOCK_UNSAFE');
    throw error;
  }
  return () => fs.rmdirSync(lockPath);
}

module.exports = {
  acquireMigrationLock,
  inspectLegacyPublishLock,
  parseMigrationOptions,
};
