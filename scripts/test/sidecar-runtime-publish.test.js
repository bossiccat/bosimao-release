'use strict';

const assert = require('node:assert/strict');
const { fork } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  createCurrentPointer,
  createRuntimeLayout,
  generationIdForProvenance,
  parseCurrentPointer,
  publishCurrentPointer,
} = require('../lib/sidecar-runtime-publish');

const workerPath = path.join(__dirname, 'sidecar-runtime-pointer-worker.js');

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-publish-'));
}

function pointer(version) {
  const provenance = Buffer.from(JSON.stringify({ schema_version: 1, version }));
  return createCurrentPointer({
    generation: generationIdForProvenance(provenance),
    manifestSha256: crypto.createHash('sha256').update(provenance).digest('hex'),
  });
}

function writePointer(root, value) {
  fs.writeFileSync(path.join(root, 'current.json'), `${JSON.stringify(value)}\n`);
}

function readPointer(root) {
  const currentPath = path.join(root, 'current.json');
  try {
    return parseCurrentPointer(JSON.parse(fs.readFileSync(currentPath, 'utf8')));
  } catch (error) {
    if (error && error.code === 'ENOENT') return null;
    throw error;
  }
}

function runPublisher(root, value, mode = 'publish') {
  return new Promise((resolve, reject) => {
    const child = fork(workerPath, [], {
      env: {
        ...process.env,
        SIDECAR_RUNTIME_DIR: root,
        SIDECAR_POINTER_JSON: JSON.stringify(value),
        SIDECAR_PUBLISH_MODE: mode,
      },
      stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
    });
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('exit', (code, signal) => resolve({ code, signal, stderr }));
  });
}

test('parent reader sees only old, new, or ENOENT during cross-process pointer replacement', async () => {
  const root = path.join(tempRoot(), 'runtime');
  createRuntimeLayout(root);
  const oldPointer = pointer('old');
  const newPointer = pointer('new');
  writePointer(root, oldPointer);
  const observed = new Set();

  const publisher = runPublisher(root, newPointer);
  for (let index = 0; index < 300; index += 1) {
    const value = readPointer(root);
    observed.add(value ? JSON.stringify(value) : 'ENOENT');
    await new Promise((resolve) => setImmediate(resolve));
  }
  const result = await publisher;

  assert.equal(result.code, 0, result.stderr);
  for (const value of observed) {
    assert.ok(value === JSON.stringify(oldPointer) || value === JSON.stringify(newPointer) || value === 'ENOENT', `invalid reader state: ${value}`);
  }
  assert.deepEqual(readPointer(root), newPointer);
  assert.deepEqual(fs.readdirSync(root).filter((entry) => entry.includes('.tmp')), []);
});

test('publisher crash before replacement leaves the old complete pointer', async () => {
  const root = path.join(tempRoot(), 'runtime');
  createRuntimeLayout(root);
  const oldPointer = pointer('old');
  const newPointer = pointer('crash-before-replace');
  writePointer(root, oldPointer);

  const result = await runPublisher(root, newPointer, 'crash-before-replace');

  assert.ok(result.signal === 'SIGKILL' || result.code !== 0, `publisher unexpectedly exited normally: ${JSON.stringify(result)}`);
  assert.deepEqual(readPointer(root), oldPointer);
  const temporaryPointers = fs.readdirSync(root).filter((entry) => entry.startsWith('current.json.'));
  assert.ok(temporaryPointers.length >= 1, 'crash should leave an uncommitted temporary pointer');
  for (const temporary of temporaryPointers) {
    assert.deepEqual(parseCurrentPointer(JSON.parse(fs.readFileSync(path.join(root, temporary), 'utf8'))), newPointer);
  }
});

test('same-volume replacement publishes a complete new pointer after temporary fsync', async () => {
  const root = path.join(tempRoot(), 'runtime');
  createRuntimeLayout(root);
  const oldPointer = pointer('old');
  const newPointer = pointer('new-same-volume');
  writePointer(root, oldPointer);

  publishCurrentPointer({ runtimeDir: root, pointer: newPointer });

  assert.deepEqual(readPointer(root), newPointer);
  assert.notDeepEqual(readPointer(root), oldPointer);
});

test('reader never treats an uncommitted temporary pointer as current', async () => {
  const root = path.join(tempRoot(), 'runtime');
  createRuntimeLayout(root);
  const oldPointer = pointer('old');
  writePointer(root, oldPointer);

  const result = await runPublisher(root, pointer('temporary-only'), 'crash-before-replace');
  assert.ok(result.signal === 'SIGKILL' || result.code !== 0, `publisher unexpectedly exited normally: ${JSON.stringify(result)}`);
  assert.deepEqual(readPointer(root), oldPointer);
});
