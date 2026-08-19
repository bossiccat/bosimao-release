'use strict';

// ADR-027 current pointer replacement adapter.
//
// On Windows this module delegates to the native `tools/sidecar-pointer-replace`
// helper (ReplaceFileW / MoveFileExW) and never falls back to fs.renameSync as
// Windows evidence. On other platforms it performs a same-volume rename plus a
// best-effort directory fsync, which is logical portability only and is not
// Windows evidence.

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const IS_WINDOWS = process.platform === 'win32';

// GetLastError values converted to decimal.
const WIN32 = {
  ERROR_FILE_NOT_FOUND: 2,
  ERROR_PATH_NOT_FOUND: 3,
  ERROR_ACCESS_DENIED: 5,
  ERROR_NOT_SAME_DEVICE: 17,
  ERROR_SHARING_VIOLATION: 32,
  ERROR_LOCK_VIOLATION: 33,
  ERROR_ALREADY_EXISTS: 183,
};

// Classified transient sharing errors eligible for bounded retry.
const TRANSIENT_CODES = new Set([
  WIN32.ERROR_SHARING_VIOLATION,
  WIN32.ERROR_LOCK_VIOLATION,
]);

const DEFAULT_MAX_ATTEMPTS = 4;
const DEFAULT_BACKOFF_MS = 20;

class PointerReplaceError extends Error {
  constructor(message, info = {}) {
    super(message);
    this.name = 'PointerReplaceError';
    if (info.operation !== undefined) this.operation = info.operation;
    if (info.nativeErrorCode !== undefined) this.nativeErrorCode = info.nativeErrorCode;
    if (info.transient !== undefined) this.transient = info.transient;
    if (info.attempts !== undefined) this.attempts = info.attempts;
    if (info.retries !== undefined) this.retries = info.retries;
    if (info.stderr !== undefined) this.stderr = info.stderr;
  }
}

function isTransientCode(code) {
  return typeof code === 'number' && TRANSIENT_CODES.has(code);
}

function sleepSync(milliseconds) {
  if (milliseconds <= 0) return;
  const buffer = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(buffer), 0, 0, milliseconds);
}

function defaultHelperCandidates() {
  const base = path.resolve(__dirname, '..', '..', 'tools', 'sidecar-pointer-replace');
  const name = IS_WINDOWS ? 'sidecar-pointer-replace.exe' : 'sidecar-pointer-replace';
  return [
    path.join(base, 'target', 'release', name),
    path.join(base, 'target', 'debug', name),
  ];
}

function resolveHelperPath(env = process.env) {
  const override = env.SIDECAR_POINTER_REPLACE_HELPER;
  if (override) return fs.existsSync(override) ? override : null;
  for (const candidate of defaultHelperCandidates()) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function validatePaths(temporaryPath, currentPath) {
  if (typeof temporaryPath !== 'string' || typeof currentPath !== 'string') {
    throw new PointerReplaceError('temporary and current pointer paths are required');
  }
  if (path.resolve(temporaryPath) === path.resolve(currentPath)) {
    throw new PointerReplaceError('temporary pointer must differ from current pointer');
  }
}

function invokeNative(helperPath, operation, temporaryPath, currentPath) {
  const spawned = spawnSync(helperPath, [operation, temporaryPath, currentPath], {
    encoding: 'utf8',
    windowsHide: true,
  });

  if (spawned.error) {
    return {
      success: false,
      operation,
      nativeErrorCode: undefined,
      transient: false,
      message: `failed to spawn native helper: ${spawned.error.message}`,
      stderr: '',
    };
  }

  let parsed;
  try {
    parsed = JSON.parse((spawned.stdout || '').trim());
  } catch {
    return {
      success: false,
      operation,
      nativeErrorCode: undefined,
      transient: false,
      message: `native helper returned non-JSON output (exit ${spawned.status})`,
      stderr: (spawned.stderr || '').trim(),
    };
  }

  if (spawned.status !== 0 || parsed.success !== true) {
    const nativeErrorCode = typeof parsed.nativeErrorCode === 'number' ? parsed.nativeErrorCode : undefined;
    return {
      success: false,
      operation,
      nativeErrorCode,
      transient: isTransientCode(nativeErrorCode),
      message: `native ${operation} failed (nativeErrorCode ${nativeErrorCode})`,
      stderr: (spawned.stderr || '').trim(),
    };
  }

  return { success: true, operation, nativeErrorCode: 0 };
}

function runWithRetry({ invoke, maxAttempts, backoffMs, isTransient }) {
  let lastFailure = null;
  let attempts = 0;
  for (attempts = 1; attempts <= maxAttempts; attempts += 1) {
    const result = invoke(attempts - 1);
    if (result.success) return result;
    lastFailure = result;
    const canRetry = isTransient(result.nativeErrorCode) && attempts < maxAttempts;
    if (!canRetry) break;
    sleepSync(backoffMs);
  }
  throw new PointerReplaceError(lastFailure.message || 'pointer replacement failed', {
    operation: lastFailure.operation,
    nativeErrorCode: lastFailure.nativeErrorCode,
    transient: lastFailure.transient,
    attempts,
    retries: attempts - 1,
    stderr: lastFailure.stderr,
  });
}

function syncDirectory(directory) {
  let descriptor;
  try {
    descriptor = fs.openSync(directory, 'r');
    fs.fsyncSync(descriptor);
  } catch {
    // Directory fsync is unsupported on some platforms; best-effort only.
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
}

function replacePointerPortable(temporaryPath, currentPath) {
  // Logical portability only; NOT Windows evidence.
  fs.renameSync(temporaryPath, currentPath);
  syncDirectory(path.dirname(currentPath));
  return { operation: 'rename', temporaryPath, currentPath, portable: true };
}

function replacePointer(input = {}) {
  const temporaryPath = input.temporaryPath;
  const currentPath = input.currentPath;
  validatePaths(temporaryPath, currentPath);

  if (!IS_WINDOWS) {
    return replacePointerPortable(temporaryPath, currentPath);
  }

  const helperPath = input.helperPath !== undefined ? input.helperPath : resolveHelperPath();
  if (!helperPath || !fs.existsSync(helperPath)) {
    throw new PointerReplaceError(
      'native pointer replace helper not found; refusing to fall back to fs.renameSync on Windows',
      { operation: input.operation || 'replace' },
    );
  }

  const operation = input.operation || (fs.existsSync(currentPath) ? 'replace' : 'create');
  const maxAttempts = input.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
  const backoffMs = input.backoffMs ?? DEFAULT_BACKOFF_MS;

  runWithRetry({
    maxAttempts,
    backoffMs,
    isTransient: isTransientCode,
    invoke: () => invokeNative(helperPath, operation, temporaryPath, currentPath),
  });

  return { operation, temporaryPath, currentPath };
}

module.exports = {
  DEFAULT_BACKOFF_MS,
  DEFAULT_MAX_ATTEMPTS,
  IS_WINDOWS,
  PointerReplaceError,
  TRANSIENT_CODES,
  WIN32,
  isTransientCode,
  replacePointer,
  resolveHelperPath,
  runWithRetry,
};
