'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  IS_WINDOWS,
  PointerReplaceError,
  WIN32,
  isTransientCode,
  replacePointer,
  runWithRetry,
} = require('../lib/sidecar-pointer-replace');

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-pointer-'));
}

function findSecondVolume() {
  if (!IS_WINDOWS) return null;
  const currentRoot = path.parse(os.tmpdir()).root.toLowerCase();
  for (let code = 67; code <= 90; code += 1) {
    const root = `${String.fromCharCode(code)}:\\`;
    if (root.toLowerCase() === currentRoot) continue;
    try {
      if (fs.statSync(root).isDirectory()) return root;
    } catch {
      // drive letter not present
    }
  }
  return null;
}

const secondVolume = findSecondVolume();

test('missing-pointer first publish uses native create (MoveFileExW)', { skip: !IS_WINDOWS }, () => {
  const dir = tempRoot();
  const currentPath = path.join(dir, 'current.json');
  const temporaryPath = path.join(dir, 'current.json.tmp');
  const bytes = '{"schema_version":1,"generation":"g-first","manifest_sha256":"a"}\n';
  fs.writeFileSync(temporaryPath, bytes);

  const result = replacePointer({ temporaryPath, currentPath });

  assert.equal(result.operation, 'create');
  assert.equal(fs.existsSync(currentPath), true);
  assert.equal(fs.existsSync(temporaryPath), false);
  assert.equal(fs.readFileSync(currentPath, 'utf8'), bytes);
});

test('existing-pointer publish uses native replace (ReplaceFileW)', { skip: !IS_WINDOWS }, () => {
  const dir = tempRoot();
  const currentPath = path.join(dir, 'current.json');
  const temporaryPath = path.join(dir, 'current.json.tmp');
  fs.writeFileSync(currentPath, 'OLD\n');
  fs.writeFileSync(temporaryPath, 'NEW\n');

  const result = replacePointer({ temporaryPath, currentPath });

  assert.equal(result.operation, 'replace');
  assert.equal(fs.readFileSync(currentPath, 'utf8'), 'NEW\n');
  assert.equal(fs.existsSync(temporaryPath), false);
});

test('native helper rejects temporary/current pointers on different NTFS volumes', { skip: !IS_WINDOWS || !secondVolume }, () => {
  const firstDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-ptr-vol-a-'));
  const otherDir = fs.mkdtempSync(path.join(secondVolume, 'jax-ptr-vol-b-'));
  const temporaryPath = path.join(firstDir, 'current.json.tmp');
  const currentPath = path.join(otherDir, 'current.json');
  fs.writeFileSync(temporaryPath, 'T\n');

  assert.throws(
    () => replacePointer({ temporaryPath, currentPath }),
    (error) => error instanceof PointerReplaceError
      && error.nativeErrorCode === WIN32.ERROR_NOT_SAME_DEVICE,
  );
  // never unlink the old pointer on failure: nothing was created on the target volume.
  assert.equal(fs.existsSync(currentPath), false);
  assert.equal(fs.existsSync(temporaryPath), true);
});

test('fails closed when the native helper is missing', { skip: !IS_WINDOWS }, () => {
  const dir = tempRoot();
  const currentPath = path.join(dir, 'current.json');
  const temporaryPath = path.join(dir, 'current.json.tmp');
  fs.writeFileSync(currentPath, 'OLD\n');
  fs.writeFileSync(temporaryPath, 'NEW\n');

  assert.throws(
    () => replacePointer({
      temporaryPath,
      currentPath,
      helperPath: path.join(dir, 'does-not-exist.exe'),
    }),
    (error) => error instanceof PointerReplaceError && /not found|missing|refus/i.test(error.message),
  );
  // old pointer remains intact and readable.
  assert.equal(fs.readFileSync(currentPath, 'utf8'), 'OLD\n');
});

test('replaceCurrentPointer delegates to the native adapter', { skip: !IS_WINDOWS }, () => {
  const dir = tempRoot();
  const currentPath = path.join(dir, 'current.json');
  const temporaryPath = path.join(dir, 'current.json.tmp');
  fs.writeFileSync(currentPath, 'OLD\n');
  fs.writeFileSync(temporaryPath, 'NEW\n');

  const { replaceCurrentPointer } = require('../lib/sidecar-runtime-publish');
  const returned = replaceCurrentPointer(temporaryPath, currentPath);

  assert.equal(returned, path.resolve(currentPath));
  assert.equal(fs.readFileSync(currentPath, 'utf8'), 'NEW\n');
  assert.equal(fs.existsSync(temporaryPath), false);
});

test('classifies only sharing/lock violations as transient', () => {
  assert.equal(isTransientCode(WIN32.ERROR_SHARING_VIOLATION), true);
  assert.equal(isTransientCode(WIN32.ERROR_LOCK_VIOLATION), true);
  assert.equal(isTransientCode(WIN32.ERROR_ACCESS_DENIED), false);
  assert.equal(isTransientCode(WIN32.ERROR_NOT_SAME_DEVICE), false);
  assert.equal(isTransientCode(WIN32.ERROR_FILE_NOT_FOUND), false);
});

test('bounded retry only retries classified transient codes', () => {
  const calls = [];
  const invoke = () => {
    calls.push(1);
    return {
      success: false,
      operation: 'replace',
      nativeErrorCode: WIN32.ERROR_SHARING_VIOLATION,
      transient: true,
      message: 'sharing violation',
    };
  };

  assert.throws(
    () => runWithRetry({ invoke, maxAttempts: 3, backoffMs: 0, isTransient: isTransientCode }),
    (error) => error instanceof PointerReplaceError
      && error.attempts === 3
      && error.transient === true,
  );
  assert.equal(calls.length, 3);
});

test('no retry for non-transient native errors', () => {
  const calls = [];
  const invoke = () => {
    calls.push(1);
    return {
      success: false,
      operation: 'replace',
      nativeErrorCode: WIN32.ERROR_ACCESS_DENIED,
      transient: false,
      message: 'access denied',
    };
  };

  assert.throws(
    () => runWithRetry({ invoke, maxAttempts: 3, backoffMs: 0, isTransient: isTransientCode }),
    (error) => error.attempts === 1,
  );
  assert.equal(calls.length, 1);
});

test('retry succeeds once the transient sharing violation clears', () => {
  let count = 0;
  const invoke = () => {
    count += 1;
    if (count < 3) {
      return {
        success: false,
        operation: 'replace',
        nativeErrorCode: WIN32.ERROR_SHARING_VIOLATION,
        transient: true,
        message: 'sharing violation',
      };
    }
    return { success: true, operation: 'replace', nativeErrorCode: 0 };
  };

  const result = runWithRetry({ invoke, maxAttempts: 3, backoffMs: 0, isTransient: isTransientCode });

  assert.equal(result.success, true);
  assert.equal(count, 3);
});
