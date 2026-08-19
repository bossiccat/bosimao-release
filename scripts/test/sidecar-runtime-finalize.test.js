'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  createRuntimeLayout,
  finalizeStagedGeneration,
  verifyFinalizedGeneration,
} = require('../lib/sidecar-runtime-publish');

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function generationFor(bytes) {
  return `g-${sha256(bytes)}`;
}

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-finalize-'));
}

function createStagedPayload(root) {
  createRuntimeLayout(root);
  const stagingDir = fs.mkdtempSync(path.join(root, 'staging', 'pending-'));
  const provenanceBytes = Buffer.from('{"schema_version":1,"build":"task2"}\n');
  const payload = {
    'jax-rtc-sidecar.exe': Buffer.from('sidecar-binary'),
    'jax-rtc-sidecar.provenance.json': provenanceBytes,
    'resources/app/native/liteav.dll': Buffer.from('liteav'),
  };
  const expectedFiles = {};
  for (const [relative, bytes] of Object.entries(payload)) {
    const target = path.join(stagingDir, ...relative.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes);
    expectedFiles[relative] = sha256(bytes);
  }
  return { stagingDir, provenanceBytes, expectedFiles };
}

test('finalize moves exact staged payload to immutable provenance generation', () => {
  const root = path.join(tempRoot(), 'jax-rtc-sidecar-runtime');
  const fixture = createStagedPayload(root);
  const finalized = finalizeStagedGeneration({ runtimeDir: root, ...fixture });
  const generation = generationFor(fixture.provenanceBytes);

  assert.equal(finalized.generation, generation);
  assert.equal(finalized.generationDir, path.join(root, 'generations', generation));
  assert.equal(fs.existsSync(fixture.stagingDir), false);
  assert.deepEqual(verifyFinalizedGeneration({ runtimeDir: root, generation }), {
    schema_version: 1,
    generation,
    manifest_sha256: sha256(fixture.provenanceBytes),
    files: fixture.expectedFiles,
  });
});

test('finalize rejects extra or missing files without creating generation', () => {
  const root = path.join(tempRoot(), 'jax-rtc-sidecar-runtime');
  const fixture = createStagedPayload(root);
  const generationDir = path.join(root, 'generations', generationFor(fixture.provenanceBytes));

  fs.writeFileSync(path.join(fixture.stagingDir, 'unexpected.dll'), 'unexpected');
  assert.throws(
    () => finalizeStagedGeneration({ runtimeDir: root, ...fixture }),
    /closed file set|unexpected|extra/i,
  );
  assert.equal(fs.existsSync(generationDir), false);

  fs.rmSync(path.join(fixture.stagingDir, 'unexpected.dll'));
  fs.rmSync(path.join(fixture.stagingDir, 'jax-rtc-sidecar.exe'));
  assert.throws(
    () => finalizeStagedGeneration({ runtimeDir: root, ...fixture }),
    /closed file set|missing/i,
  );
  assert.equal(fs.existsSync(generationDir), false);
});

test('finalize rejects symbolic-link payload entries without following them', () => {
  const root = path.join(tempRoot(), 'jax-rtc-sidecar-runtime');
  const fixture = createStagedPayload(root);
  const outside = path.join(tempRoot(), 'outside.dll');
  const linked = path.join(fixture.stagingDir, 'resources', 'linked.dll');
  fs.writeFileSync(outside, 'outside');
  try {
    fs.symlinkSync(outside, linked, 'file');
  } catch (error) {
    throw new Error(`symlink fixture creation failed: ${error.code || error.message}`, { cause: error });
  }

  fixture.expectedFiles['resources/linked.dll'] = sha256(Buffer.from('outside'));
  assert.throws(
    () => finalizeStagedGeneration({ runtimeDir: root, ...fixture }),
    /reparse|symbolic link/i,
  );
  assert.equal(fs.existsSync(path.join(root, 'generations', generationFor(fixture.provenanceBytes))), false);
});

test('finalize refuses to overwrite an existing immutable generation', () => {
  const root = path.join(tempRoot(), 'jax-rtc-sidecar-runtime');
  const first = createStagedPayload(root);
  const finalized = finalizeStagedGeneration({ runtimeDir: root, ...first });
  const second = createStagedPayload(root);

  assert.throws(
    () => finalizeStagedGeneration({
      runtimeDir: root,
      stagingDir: second.stagingDir,
      provenanceBytes: first.provenanceBytes,
      expectedFiles: second.expectedFiles,
    }),
    /already exists|immutable/i,
  );
  assert.equal(fs.existsSync(finalized.generationDir), true);
  assert.equal(fs.existsSync(second.stagingDir), true);
});
