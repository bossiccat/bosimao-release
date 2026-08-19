'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  acquireGenerationLease,
  acquireReaderLease,
  gcGenerations,
  releaseGenerationLease,
  withExclusiveReaderGc,
} = require('../lib/sidecar-runtime-publish');
const {
  createCurrentPointer,
  createRuntimeLayout,
  generationIdForProvenance,
  publishCurrentPointer,
} = require('../lib/sidecar-runtime-publish');

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-lease-gc-'));
}

function generation(version) {
  return generationIdForProvenance(Buffer.from(`{"version":"${version}"}\n`));
}

function setupRuntime() {
  const runtimeDir = path.join(tempRoot(), 'runtime');
  createRuntimeLayout(runtimeDir);
  return runtimeDir;
}

function createGeneration(runtimeDir, id, mtime = Date.now() - 60_000) {
  const generationDir = path.join(runtimeDir, 'generations', id);
  fs.mkdirSync(generationDir, { recursive: true });
  fs.writeFileSync(path.join(generationDir, 'generation.json'), JSON.stringify({ generation: id }));
  fs.utimesSync(generationDir, new Date(mtime), new Date(mtime));
  return generationDir;
}

function setCurrent(runtimeDir, id) {
  const manifestSha256 = crypto.createHash('sha256').update(id).digest('hex');
  publishCurrentPointer({
    runtimeDir,
    pointer: createCurrentPointer({ generation: id, manifestSha256 }),
  });
}

function identityResolver(expected = new Map()) {
  return (pid) => expected.get(pid) || null;
}

function currentIdentity() {
  return { creationTime: '2026-08-17T00:00:00.000Z', identityToken: 'token-current' };
}

function oldLease(runtimeDir, id, name, fields) {
  const dir = path.join(runtimeDir, 'leases', id);
  fs.mkdirSync(dir, { recursive: true });
  const leasePath = path.join(dir, `${name}.json`);
  fs.writeFileSync(leasePath, JSON.stringify(fields));
  return leasePath;
}

test('acquire lease writes generation, PID, creation identity, and timestamps', async () => {
  const runtimeDir = setupRuntime();
  const id = generation('lease-fields');
  createGeneration(runtimeDir, id);

  const lease = await acquireGenerationLease({
    runtimeDir,
    generation: id,
    processIdentity: currentIdentity(),
  });
  const saved = JSON.parse(fs.readFileSync(lease.path, 'utf8'));

  assert.equal(saved.schema_version, 1);
  assert.equal(saved.generation, id);
  assert.equal(saved.reader_pid, process.pid);
  assert.equal(saved.process_creation_time, currentIdentity().creationTime);
  assert.equal(saved.process_creation_identity, currentIdentity().identityToken);
  assert.match(saved.created_at, /^\d{4}-\d\d-\d\dT/);
  assert.equal(saved.timestamp, saved.created_at);
  await releaseGenerationLease(lease);
  assert.equal(fs.existsSync(lease.path), false);
});

test('active lease prevents GC, then release allows non-current generation collection', async () => {
  const runtimeDir = setupRuntime();
  const old = generation('active-lease');
  const current = generation('active-current');
  createGeneration(runtimeDir, old);
  createGeneration(runtimeDir, current);
  setCurrent(runtimeDir, current);
  const identities = new Map([[process.pid, currentIdentity()]]);
  const lease = await acquireGenerationLease({ runtimeDir, generation: old, processIdentity: currentIdentity() });

  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: identityResolver(identities) });
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', old)), true);

  await releaseGenerationLease(lease);
  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: identityResolver(identities) });
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', old)), false);
});

test('GC never removes the current generation even with no retention', async () => {
  const runtimeDir = setupRuntime();
  const current = generation('never-remove-current');
  createGeneration(runtimeDir, current);
  setCurrent(runtimeDir, current);

  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: identityResolver() });
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', current)), true);
});

test('stale lease cleanup requires PID and process creation identity evidence', async () => {
  const runtimeDir = setupRuntime();
  const old = generation('stale-identity');
  const current = generation('stale-current');
  createGeneration(runtimeDir, old);
  createGeneration(runtimeDir, current);
  setCurrent(runtimeDir, current);
  const createdAt = '2020-01-01T00:00:00.000Z';
  const leaseBase = {
    schema_version: 1,
    generation: old,
    reader_pid: 424242,
    process_creation_time: '2020-01-01T00:00:00.000Z',
    process_creation_identity: 'old-token',
    created_at: createdAt,
    timestamp: createdAt,
  };
  const leasePath = oldLease(runtimeDir, old, 'pid-reused', leaseBase);
  const identities = new Map([[424242, currentIdentity()]]);

  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: identityResolver(identities), now: Date.now() });
  assert.equal(fs.existsSync(leasePath), false);
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', old)), false);

  const uncertain = generation('uncertain-identity');
  createGeneration(runtimeDir, uncertain);
  oldLease(runtimeDir, uncertain, 'unknown', { ...leaseBase, generation: uncertain, process_creation_identity: 'unknown-token' });
  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: () => null, now: Date.now() });
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', uncertain)), true);
});

test('reader shared barrier wins against GC exclusive acquisition', async () => {
  const runtimeDir = setupRuntime();
  const old = generation('barrier-old');
  const current = generation('barrier-new');
  createGeneration(runtimeDir, old);
  createGeneration(runtimeDir, current);
  setCurrent(runtimeDir, old);
  let releasePause;
  const paused = new Promise((resolve) => { releasePause = resolve; });
  let gcStarted = false;
  const reader = acquireReaderLease({
    runtimeDir,
    processIdentity: currentIdentity(),
    beforeLease: async (snapshot) => {
      assert.equal(snapshot.generation, old);
      await paused;
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  setCurrent(runtimeDir, current);
  const gc = gcGenerations({
    runtimeDir,
    retainCount: 0,
    minAgeMs: 0,
    processIdentityResolver: identityResolver(new Map([[process.pid, currentIdentity()]])),
    onExclusive: () => { gcStarted = true; },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(gcStarted, false);
  releasePause();
  const lease = await reader;
  await gc;
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', old)), true);
  await releaseGenerationLease(lease);
  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: identityResolver(new Map([[process.pid, currentIdentity()]])) });
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', old)), false);
});

test('malformed or half-written lease is retained fail-closed', async () => {
  const runtimeDir = setupRuntime();
  const old = generation('half-written-lease');
  const current = generation('half-written-current');
  createGeneration(runtimeDir, old);
  createGeneration(runtimeDir, current);
  setCurrent(runtimeDir, current);
  const leaseDir = path.join(runtimeDir, 'leases', old);
  fs.mkdirSync(leaseDir, { recursive: true });
  const leasePath = path.join(leaseDir, 'half-written.json');
  fs.writeFileSync(leasePath, '{"schema_version":1,"generation":');

  await gcGenerations({ runtimeDir, retainCount: 0, minAgeMs: 0, processIdentityResolver: () => null });
  assert.equal(fs.existsSync(leasePath), true);
  assert.equal(fs.existsSync(path.join(runtimeDir, 'generations', old)), true);
});

test('exclusive reader GC helper serializes a callback with readers', async () => {
  const runtimeDir = setupRuntime();
  let ran = false;
  await withExclusiveReaderGc(runtimeDir, async () => { ran = true; });
  assert.equal(ran, true);
});
