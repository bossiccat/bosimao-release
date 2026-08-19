'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { makeImmutableGeneration, restoreWritableGeneration } = require('./sidecar-runtime-immutable');
const {
  assertExactKeys,
  createRuntimeLayout,
  fail,
  parseCurrentPointer,
  parseGenerationId,
  writeAndSyncFile,
  GENERATION_RE,
} = require('./sidecar-runtime-protocol');

const readerGcStates = new Map();

function runtimeState(runtimeDir) {
  let state = readerGcStates.get(path.resolve(runtimeDir));
  if (!state) {
    state = { readers: 0, exclusive: false, waiters: [] };
    readerGcStates.set(path.resolve(runtimeDir), state);
  }
  return state;
}

function waitForReaderGcShared(runtimeDir) {
  const state = runtimeState(runtimeDir);
  if (!state.exclusive && state.waiters.length === 0) {
    state.readers += 1;
    return Promise.resolve(() => {
      state.readers -= 1;
      drainReaderGc(runtimeDir);
    });
  }
  return new Promise((resolve) => state.waiters.push({ mode: 'shared', resolve }));
}

function drainReaderGc(runtimeDir) {
  const state = runtimeState(runtimeDir);
  if (state.exclusive || state.readers > 0) return;
  const exclusive = state.waiters.find((waiter) => waiter.mode === 'exclusive');
  if (exclusive) {
    state.waiters = state.waiters.filter((waiter) => waiter !== exclusive);
    state.exclusive = true;
    exclusive.resolve(() => {
      state.exclusive = false;
      drainReaderGc(runtimeDir);
    });
    return;
  }
  const shared = state.waiters.filter((waiter) => waiter.mode === 'shared');
  state.waiters = [];
  state.readers += shared.length;
  for (const waiter of shared) waiter.resolve(() => {
    state.readers -= 1;
    drainReaderGc(runtimeDir);
  });
}

function waitForReaderGcExclusive(runtimeDir) {
  const state = runtimeState(runtimeDir);
  if (!state.exclusive && state.readers === 0) {
    state.exclusive = true;
    return Promise.resolve(() => {
      state.exclusive = false;
      drainReaderGc(runtimeDir);
    });
  }
  return new Promise((resolve) => state.waiters.push({ mode: 'exclusive', resolve }));
}

async function withSharedReaderGc(runtimeDir, callback) {
  const release = await waitForReaderGcShared(runtimeDir);
  try { return await callback(); } finally { release(); }
}

async function withExclusiveReaderGc(runtimeDir, callback) {
  const release = await waitForReaderGcExclusive(runtimeDir);
  try { return await callback(); } finally { release(); }
}

function leaseIdentity(input) {
  if (!input || typeof input !== 'object') fail('process identity required');
  if (!Number.isInteger(input.pid) || input.pid <= 0) fail('process PID required');
  if (typeof input.creationTime !== 'string' || input.creationTime.length === 0) fail('process creation time required');
  if (typeof input.identityToken !== 'string' || input.identityToken.length === 0) fail('process identity token required');
  return {
    pid: input.pid,
    creationTime: input.creationTime,
    identityToken: input.identityToken,
  };
}

function validateLease(value, generation) {
  assertExactKeys(value, [
    'schema_version', 'generation', 'reader_pid', 'process_creation_time',
    'process_creation_identity', 'created_at', 'timestamp',
  ], 'lease');
  if (value.schema_version !== 1 || value.generation !== generation) fail('invalid lease');
  if (!Number.isInteger(value.reader_pid) || value.reader_pid <= 0) fail('invalid lease PID');
  if (typeof value.process_creation_time !== 'string' || !value.process_creation_time) fail('invalid lease creation time');
  if (typeof value.process_creation_identity !== 'string' || !value.process_creation_identity) fail('invalid lease identity');
  if (typeof value.created_at !== 'string' || typeof value.timestamp !== 'string') fail('invalid lease timestamp');
  return value;
}

async function acquireGenerationLease({ runtimeDir, generation, processIdentity = {}, readerId = crypto.randomUUID() }) {
  parseGenerationId(generation);
  const identity = leaseIdentity({ pid: process.pid, ...processIdentity });
  createRuntimeLayout(runtimeDir);
  const leaseDir = path.join(runtimeDir, 'leases', generation);
  fs.mkdirSync(leaseDir, { recursive: true });
  const leasePath = path.join(leaseDir, `${readerId}.json`);
  const now = new Date().toISOString();
  const payload = {
    schema_version: 1,
    generation,
    reader_pid: identity.pid,
    process_creation_time: identity.creationTime,
    process_creation_identity: identity.identityToken,
    created_at: now,
    timestamp: now,
  };
  validateLease(payload, generation);
  writeAndSyncFile(leasePath, Buffer.from(`${JSON.stringify(payload)}\n`));
  return { runtimeDir, generation, path: leasePath, payload };
}

async function acquireReaderLease({ runtimeDir, processIdentity, beforeLease }) {
  return withSharedReaderGc(runtimeDir, async () => {
    const pointer = parseCurrentPointer(JSON.parse(fs.readFileSync(path.join(runtimeDir, 'current.json'), 'utf8')));
    if (beforeLease) await beforeLease(pointer);
    return acquireGenerationLease({ runtimeDir, generation: pointer.generation, processIdentity });
  });
}

async function releaseGenerationLease(lease) {
  if (!lease || typeof lease.path !== 'string') fail('lease required');
  fs.rmSync(lease.path, { force: false });
  try {
    const directory = path.dirname(lease.path);
    if (fs.readdirSync(directory).length === 0) fs.rmdirSync(directory);
  } catch (error) {
    if (error.code !== 'ENOENT' && error.code !== 'ENOTEMPTY') throw error;
  }
}

function readCurrentGeneration(runtimeDir) {
  try {
    return parseCurrentPointer(JSON.parse(fs.readFileSync(path.join(runtimeDir, 'current.json'), 'utf8'))).generation;
  } catch (error) {
    return null;
  }
}

function leaseIsStale(lease, processIdentityResolver) {
  let identity;
  try { identity = processIdentityResolver(lease.reader_pid); } catch { return false; }
  if (!identity || typeof identity !== 'object') return false;
  return identity.creationTime !== lease.process_creation_time
    || identity.identityToken !== lease.process_creation_identity;
}

function leasesForGeneration(runtimeDir, generation, processIdentityResolver) {
  const directory = path.join(runtimeDir, 'leases', generation);
  if (!fs.existsSync(directory)) return { active: false, uncertain: false };
  let active = false;
  let uncertain = false;
  for (const name of fs.readdirSync(directory)) {
    if (!name.endsWith('.json')) { uncertain = true; continue; }
    const leasePath = path.join(directory, name);
    let lease;
    try {
      lease = validateLease(JSON.parse(fs.readFileSync(leasePath, 'utf8')), generation);
    } catch {
      uncertain = true;
      continue;
    }
    if (leaseIsStale(lease, processIdentityResolver)) {
      try { fs.rmSync(leasePath); } catch { uncertain = true; }
    } else {
      active = true;
    }
  }
  return { active, uncertain };
}

async function gcGenerations({
  runtimeDir,
  retainCount = 2,
  minAgeMs = 0,
  processIdentityResolver = () => null,
  now = Date.now(),
  onExclusive,
  deleteGeneration = (generationDir) => fs.rmSync(generationDir, { recursive: true, force: false }),
}) {
  return withExclusiveReaderGc(runtimeDir, async () => {
    if (onExclusive) onExclusive();
    const current = readCurrentGeneration(runtimeDir);
    if (!current) return { removed: [], retained: [] };
    const entries = fs.readdirSync(path.join(runtimeDir, 'generations'), { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && GENERATION_RE.test(entry.name))
      .map((entry) => entry.name);
    const ordered = entries.filter((entry) => entry !== current).sort().reverse();
    const retained = new Set(ordered.slice(0, retainCount));
    const removed = [];
    for (const id of ordered) {
      if (retained.has(id)) continue;
      const generationDir = path.join(runtimeDir, 'generations', id);
      const age = now - fs.statSync(generationDir).mtimeMs;
      if (age < minAgeMs) continue;
      const leaseState = leasesForGeneration(runtimeDir, id, processIdentityResolver);
      if (leaseState.active || leaseState.uncertain) continue;
      if (readCurrentGeneration(runtimeDir) === id) continue;
      try {
        restoreWritableGeneration(generationDir);
        deleteGeneration(generationDir);
        removed.push(id);
      } catch {
        // 删除失败时 generation 仍会保留；恢复其只读保护，等待后续 GC 再重试。
        try { makeImmutableGeneration(generationDir); } catch { /* preserve later GC retry */ }
      }
    }
    return { removed, retained: [...retained] };
  });
}

module.exports = {
  acquireGenerationLease,
  acquireReaderLease,
  gcGenerations,
  releaseGenerationLease,
  withExclusiveReaderGc,
};
