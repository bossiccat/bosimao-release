'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  assertStableRoot,
  createCurrentPointer,
  createGenerationMetadata,
  createRuntimeLayout,
  generationIdForProvenance,
  parseCurrentPointer,
  parseGenerationId,
} = require('../lib/sidecar-runtime-publish');

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-protocol-'));
}

function provenance(version = 'old') {
  return Buffer.from(JSON.stringify({ schema_version: 1, version }));
}

function digest(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

test('stable root is never renamed and has the exact protocol layout', () => {
  const root = path.join(tempRoot(), 'jax-rtc-sidecar-runtime');
  const before = root;
  createRuntimeLayout(root);

  assert.equal(fs.realpathSync(root), fs.realpathSync(before));
  assert.deepEqual(
    fs.readdirSync(root).sort(),
    ['current.json', 'generations', 'leases', 'publish.lock', 'reader-gc.lock', 'staging'],
  );
  assert.equal(fs.statSync(path.join(root, 'generations')).isDirectory(), true);
  assert.equal(fs.statSync(path.join(root, 'staging')).isDirectory(), true);
  assert.equal(fs.statSync(path.join(root, 'leases')).isDirectory(), true);
  assert.equal(fs.statSync(path.join(root, 'publish.lock')).isFile(), true);
  assert.equal(fs.statSync(path.join(root, 'reader-gc.lock')).isFile(), true);
  assert.equal(fs.statSync(path.join(root, 'current.json')).isFile(), true);
});

test('generation id is g plus the provenance byte SHA-256', () => {
  const bytes = provenance('new');
  const id = generationIdForProvenance(bytes);

  assert.match(id, /^g-[0-9a-f]{64}$/);
  assert.equal(id, `g-${digest(bytes)}`);
  assert.equal(parseGenerationId(id), id);
});

test('generation metadata has the exact schema and closed file hash set', () => {
  const bytes = provenance('new');
  const generation = generationIdForProvenance(bytes);
  const files = {
    'jax-rtc-sidecar.exe': 'a'.repeat(64),
    'jax-rtc-sidecar.exe.sha256': 'b'.repeat(64),
    'jax-rtc-sidecar.provenance.json': digest(bytes),
    'jax-rtc-sidecar.provenance.sha256': 'c'.repeat(64),
    'resources/app/native/liteav.dll': 'd'.repeat(64),
  };
  const metadata = createGenerationMetadata({
    generation,
    manifestSha256: digest(bytes),
    files,
  });

  assert.deepEqual(Object.keys(metadata).sort(), ['files', 'generation', 'manifest_sha256', 'schema_version']);
  assert.equal(metadata.schema_version, 1);
  assert.equal(metadata.generation, generation);
  assert.equal(metadata.manifest_sha256, digest(bytes));
  assert.deepEqual(Object.keys(metadata.files).sort(), Object.keys(files).sort());
  assert.deepEqual(metadata.files, files);
  assert.throws(
    () => createGenerationMetadata({ generation, manifestSha256: digest(bytes), files, extra: 'forbidden' }),
    /unknown generation metadata field|exact generation metadata/i,
  );
});

test('current pointer has the exact schema and accepts only a valid generation', () => {
  const bytes = provenance('new');
  const generation = generationIdForProvenance(bytes);
  const pointer = createCurrentPointer({ generation, manifestSha256: digest(bytes) });

  assert.deepEqual(Object.keys(pointer).sort(), ['generation', 'manifest_sha256', 'schema_version']);
  assert.deepEqual(parseCurrentPointer(pointer), pointer);
  assert.throws(
    () => parseCurrentPointer({ ...pointer, runtime_dir: '/legacy/path' }),
    /unknown current pointer field|exact current pointer/i,
  );
  assert.throws(
    () => parseCurrentPointer({ ...pointer, generation: '2026-08-17T00:00:00.000Z' }),
    /invalid generation/i,
  );
});

test('flat runtime and timestamp or UUID generation layouts are rejected', () => {
  const root = path.join(tempRoot(), 'jax-rtc-sidecar-runtime');
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, 'jax-rtc-sidecar.exe'), 'legacy');

  assert.throws(() => assertStableRoot(root), /flat runtime|stable root|legacy/i);
  assert.throws(() => parseGenerationId('2026-08-17T00-00-00.000Z'), /invalid generation/i);
  assert.throws(() => parseGenerationId('g-550e8400-e29b-41d4-a716-446655440000'), /invalid generation/i);
});
