'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  acquireRuntimeLease,
  coordinationLockPath,
  runtimeCoordinationIdentity,
} = require('../lib/sidecar-runtime-coordination');

const runtimeDir = 'C:\\Program Files\\Jax\\binaries\\jax-rtc-sidecar-runtime';

function tempIdentity() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'coordination-'));
  return { root: dir, runtimeDir: path.join(dir, 'jax-rtc-sidecar-runtime') };
}

function liveProcess(options = {}) {
  return () => ({
    status: 'alive',
    pid: options.pid || 4242,
    creationTime: options.creationTime || '2026-08-29T10:00:00Z',
  });
}

function deadProcess() {
  return () => ({ status: 'absent' });
}

function unavailableProcess() {
  return () => ({ status: 'unknown' });
}

test('runtime identity is the stable parent plus fixed runtime name', () => {
  assert.equal(
    runtimeCoordinationIdentity(runtimeDir),
    'c:\\program files\\jax\\binaries\\jax-rtc-sidecar-runtime',
  );
});

test('identity rejects a drive root and an empty runtime name', () => {
  assert.equal(runtimeCoordinationIdentity('C:\\'), '');
  assert.equal(runtimeCoordinationIdentity(''), '');
});

test('identity resolves the runtime parent before canonicalising the lock name', () => {
  const seen = [];
  const identity = runtimeCoordinationIdentity(runtimeDir, {
    realpath(value) {
      seen.push(value);
      return 'C:\\Real\\Binaries';
    },
  });
  assert.deepEqual(seen, ['C:\\Program Files\\Jax\\binaries']);
  assert.equal(identity, 'c:\\real\\binaries\\jax-rtc-sidecar-runtime');
});

test('identity uses the native realpath so 8.3 short names collapse to one lock', {
  skip: process.platform !== 'win32' ? 'windows-only path spelling evidence' : false,
}, () => {
  const original = fs.realpathSync.native;
  const seen = [];
  fs.realpathSync.native = (value) => {
    seen.push(value);
    return 'C:\\Real\\Binaries';
  };
  let identity;
  try {
    identity = runtimeCoordinationIdentity(runtimeDir);
  } finally {
    fs.realpathSync.native = original;
  }
  assert.deepEqual(seen, ['C:\\Program Files\\Jax\\binaries']);
  assert.equal(identity, 'c:\\real\\binaries\\jax-rtc-sidecar-runtime');
});

test('identity falls back to the given parent when it cannot be resolved', () => {
  assert.equal(
    runtimeCoordinationIdentity(runtimeDir, {
      realpath() {
        throw new Error('ENOENT');
      },
    }),
    'c:\\program files\\jax\\binaries\\jax-rtc-sidecar-runtime',
  );
});

test('acquire writes a complete owner record and release clears it', () => {
  const { runtimeDir: target } = tempIdentity();
  const release = acquireRuntimeLease(target, {
    operation: 'publish',
    token: '11111111-2222-4333-8444-555555555555',
    pid: 4242,
    processCreationTime: '2026-08-29T10:00:00Z',
    createdAt: '2026-08-29T10:05:00Z',
    inspectProcess: liveProcess(),
  });
  const lockFile = coordinationLockPath(target);
  const owner = JSON.parse(fs.readFileSync(lockFile, 'utf8'));
  assert.equal(owner.schema_version, 1);
  assert.equal(owner.token, '11111111-2222-4333-8444-555555555555');
  assert.equal(owner.pid, 4242);
  assert.equal(owner.operation, 'publish');
  assert.equal(owner.runtime_name, 'jax-rtc-sidecar-runtime');
  assert.equal(owner.process_creation_time, '2026-08-29T10:00:00Z');
  assert.ok(owner.process_creation_identity.length > 0);
  release();
  assert.equal(fs.existsSync(lockFile), false);
});

test('a second acquire fails closed while a live owner holds the lease', () => {
  const { runtimeDir: target } = tempIdentity();
  const created = '2026-08-29T10:00:00Z';
  const release = acquireRuntimeLease(target, {
    processCreationTime: created,
    inspectProcess: liveProcess({ creationTime: created }),
  });
  assert.throws(
    () => acquireRuntimeLease(target, { inspectProcess: liveProcess({ creationTime: created }) }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_BUSY',
  );
  release();
});

test('a live pid whose creation time differs is treated as pid reuse', () => {
  const { runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, {
    processCreationTime: '2026-08-29T10:00:00Z',
    inspectProcess: liveProcess({ creationTime: '2026-08-29T10:00:00Z' }),
  });
  assert.throws(
    () => acquireRuntimeLease(target, { inspectProcess: liveProcess({ creationTime: '2026-08-30T09:00:00Z' }) }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_PID_REUSED',
  );
});

test('a stale owner is not reclaimed automatically', () => {
  const { runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  const lockFile = coordinationLockPath(target);
  fs.writeFileSync(lockFile, JSON.stringify({
    schema_version: 1,
    token: '99999999-2222-4333-8444-555555555555',
    pid: 4242,
    created_at: '2026-08-29T10:05:00Z',
    process_creation_time: '2026-08-29T10:00:00Z',
    process_creation_identity: 'win32:x64',
    operation: 'publish',
    runtime_name: 'jax-rtc-sidecar-runtime',
  }));
  assert.throws(
    () => acquireRuntimeLease(target, { inspectProcess: deadProcess() }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_STALE_OWNER',
  );
});

test('an owner record that cannot be validated is ambiguous', () => {
  const { runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  fs.writeFileSync(coordinationLockPath(target), '{"token":"not-a-uuid"}');
  assert.throws(
    () => acquireRuntimeLease(target, { inspectProcess: deadProcess() }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_OWNER_AMBIGUOUS',
  );
});

test('an unavailable process probe is not treated as a free lock', () => {
  const { runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  assert.throws(
    () => acquireRuntimeLease(target, { inspectProcess: unavailableProcess() }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_PROBE_UNAVAILABLE',
  );
});

test('release fails closed when the owner record no longer belongs to the lease', () => {
  const { runtimeDir: target } = tempIdentity();
  const release = acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  const lockFile = coordinationLockPath(target);
  const owner = JSON.parse(fs.readFileSync(lockFile, 'utf8'));
  owner.token = 'aaaaaaaa-2222-4333-8444-555555555555';
  fs.writeFileSync(lockFile, JSON.stringify(owner));
  assert.throws(
    () => release(),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_LEASE_LOST',
  );
});

test('the lock is a sibling of the runtime root so a root rename preserves it', () => {
  const { root, runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  const lockFile = coordinationLockPath(target);
  const resolve = typeof fs.realpathSync.native === 'function'
    ? (value) => fs.realpathSync.native(value)
    : (value) => fs.realpathSync(value);
  assert.equal(resolve(path.dirname(lockFile)).toLowerCase(), resolve(root).toLowerCase());
  assert.equal(fs.existsSync(lockFile), true);
});

test('release fails closed when the owner record cannot be removed', () => {
  const { runtimeDir: target } = tempIdentity();
  const release = acquireRuntimeLease(target, {
    inspectProcess: liveProcess(),
    unlink() {
      const error = new Error('EPERM');
      error.code = 'SOMETHING_NONTRANSIENT';
      throw error;
    },
  });
  assert.throws(
    () => release(),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_RELEASE_FAILED',
  );
});

test('release retries transient unlink failures before succeeding', () => {
  const { runtimeDir: target } = tempIdentity();
  const lockFile = coordinationLockPath(target);
  let attempts = 0;
  const realUnlink = fs.unlinkSync.bind(fs);
  const release = acquireRuntimeLease(target, {
    inspectProcess: liveProcess(),
    retryDelayMs: 0,
    unlink(file) {
      attempts += 1;
      if (attempts <= 2) {
        const error = new Error('resource busy');
        error.code = 'EBUSY';
        throw error;
      }
      return realUnlink(file);
    },
  });
  release();
  assert.equal(attempts, 3);
  assert.equal(fs.existsSync(lockFile), false);
});

test('release gives up after bounded attempts and preserves the last errno', () => {
  const { runtimeDir: target } = tempIdentity();
  let attempts = 0;
  const release = acquireRuntimeLease(target, {
    inspectProcess: liveProcess(),
    retryDelayMs: 0,
    unlink() {
      attempts += 1;
      const error = new Error('resource busy');
      error.code = 'EBUSY';
      throw error;
    },
  });
  assert.throws(
    () => release(),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_RELEASE_FAILED'
      && error.last_errno_code === 'EBUSY'
      && error.attempts === 5,
  );
  assert.equal(attempts, 5);
});

test('release does not retry non-transient unlink failures', () => {
  const { runtimeDir: target } = tempIdentity();
  let attempts = 0;
  const release = acquireRuntimeLease(target, {
    inspectProcess: liveProcess(),
    retryDelayMs: 0,
    unlink() {
      attempts += 1;
      const error = new Error('read-only file system');
      error.code = 'EROFS';
      throw error;
    },
  });
  assert.throws(
    () => release(),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_RELEASE_FAILED'
      && error.last_errno_code === 'EROFS'
      && error.attempts === 1,
  );
  assert.equal(attempts, 1);
});


test('a missing runtime parent yields a stable diagnostic instead of raw ENOENT', () => {
  const { root } = tempIdentity();
  const missing = path.join(root, 'no-such-parent', 'jax-rtc-sidecar-runtime');
  assert.throws(
    () => acquireRuntimeLease(missing, { inspectProcess: liveProcess() }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_RUNTIME_PARENT_MISSING',
  );
});


test('a probe that times out once is retried instead of reported unavailable', () => {
  // 2026-09-24：CI 上 Get-CimInstance 冷启动打满原 5s 预算 ⇒ spawnSync 返回 ETIMEDOUT
  // ⇒ 探针退化成 {status:'unknown'} ⇒ 合法迁移被 PROBE_UNAVAILABLE 挡掉。
  // 这里用"第一次超时、第二次成功"的注入 runner 钉住重试行为。
  const { runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  const owner = JSON.parse(fs.readFileSync(coordinationLockPath(target), 'utf8'));

  let calls = 0;
  const flaky = () => {
    calls += 1;
    if (calls === 1) {
      const error = new Error('spawnSync powershell.exe ETIMEDOUT');
      error.code = 'ETIMEDOUT';
      return { error, status: null, stdout: '', signal: 'SIGTERM' };
    }
    return {
      status: 0,
      signal: null,
      stdout: JSON.stringify({
        status: 'alive', pid: owner.pid, creationTime: owner.process_creation_time,
      }),
    };
  };

  assert.throws(
    () => acquireRuntimeLease(target, { runPowerShell: flaky }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_BUSY',
  );
  assert.equal(calls, 2, '第一次超时后必须重试一次，而不是直接判 PROBE_UNAVAILABLE');
});

test('a probe that times out every time still fails closed as unavailable', () => {
  // 阴性对照：重试不得把"探针真的跑不成"变成"锁可用"。
  const { runtimeDir: target } = tempIdentity();
  acquireRuntimeLease(target, { inspectProcess: liveProcess() });
  let calls = 0;
  const alwaysTimeout = () => {
    calls += 1;
    const error = new Error('spawnSync powershell.exe ETIMEDOUT');
    error.code = 'ETIMEDOUT';
    return { error, status: null, stdout: '', signal: 'SIGTERM' };
  };
  assert.throws(
    () => acquireRuntimeLease(target, { runPowerShell: alwaysTimeout }),
    (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_PROBE_UNAVAILABLE',
  );
  assert.equal(calls, 2, '重试次数应为 2，不得无限重试');
});
