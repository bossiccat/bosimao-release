'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const GENERATION_RE = /^g-[0-9a-f]{64}$/;
const HASH_RE = /^[0-9a-f]{64}$/;
const CURRENT_KEYS = ['schema_version', 'generation', 'manifest_sha256'];
const GENERATION_KEYS = ['schema_version', 'generation', 'manifest_sha256', 'files'];
const LAYOUT_ENTRIES = [
  'current.json',
  'generations',
  'leases',
  'publish.lock',
  'reader-gc.lock',
  'staging',
];

function fail(message) {
  throw new Error(message);
}

function assertHash(value, label) {
  if (typeof value !== 'string' || !HASH_RE.test(value)) fail(`invalid ${label}`);
  return value;
}

function parseGenerationId(value) {
  if (typeof value !== 'string' || !GENERATION_RE.test(value)) fail(`invalid generation id: ${value}`);
  return value;
}

function generationIdForProvenance(provenanceBytes) {
  if (!Buffer.isBuffer(provenanceBytes) && !(provenanceBytes instanceof Uint8Array)) {
    fail('provenance must be bytes');
  }
  return `g-${crypto.createHash('sha256').update(provenanceBytes).digest('hex')}`;
}

function assertExactKeys(value, expected, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`invalid ${label}`);
  const actual = Object.keys(value).sort();
  const keys = [...expected].sort();
  if (actual.length !== keys.length || actual.some((key, index) => key !== keys[index])) {
    fail(`unknown ${label} field or exact ${label} schema required`);
  }
}

function createGenerationMetadata(input) {
  if (!input || typeof input !== 'object') fail('generation metadata input required');
  assertExactKeys(input, ['generation', 'manifestSha256', 'files'], 'generation metadata input');
  const generation = parseGenerationId(input.generation);
  const manifestSha256 = assertHash(input.manifestSha256, 'manifest SHA-256');
  if (!input.files || typeof input.files !== 'object' || Array.isArray(input.files)) {
    fail('invalid generation metadata files');
  }
  const files = {};
  for (const relative of Object.keys(input.files).sort()) {
    if (!relative || path.isAbsolute(relative) || relative.includes('\\') || relative.split('/').includes('..')) {
      fail(`invalid generation metadata path: ${relative}`);
    }
    files[relative] = assertHash(input.files[relative], `file SHA-256 for ${relative}`);
  }
  const metadata = {
    schema_version: 1,
    generation,
    manifest_sha256: manifestSha256,
    files,
  };
  assertExactKeys(metadata, GENERATION_KEYS, 'generation metadata');
  return metadata;
}

function createCurrentPointer(input) {
  if (!input || typeof input !== 'object') fail('current pointer input required');
  assertExactKeys(input, ['generation', 'manifestSha256'], 'current pointer input');
  return {
    schema_version: 1,
    generation: parseGenerationId(input.generation),
    manifest_sha256: assertHash(input.manifestSha256, 'manifest SHA-256'),
  };
}

function parseCurrentPointer(pointer) {
  assertExactKeys(pointer, CURRENT_KEYS, 'current pointer');
  if (pointer.schema_version !== 1) fail('invalid current pointer schema version');
  return createCurrentPointer({
    generation: pointer.generation,
    manifestSha256: pointer.manifest_sha256,
  });
}

function createRuntimeLayout(runtimeRoot) {
  if (typeof runtimeRoot !== 'string' || runtimeRoot.length === 0) fail('stable root is required');
  fs.mkdirSync(runtimeRoot, { recursive: true });
  for (const entry of LAYOUT_ENTRIES) {
    const target = path.join(runtimeRoot, entry);
    if (entry.endsWith('.json')) fs.closeSync(fs.openSync(target, 'a'));
    else if (entry.endsWith('.lock')) fs.closeSync(fs.openSync(target, 'a'));
    else fs.mkdirSync(target, { recursive: true });
  }
  assertStableRoot(runtimeRoot);
  return runtimeRoot;
}

function assertStableRoot(runtimeRoot) {
  if (!fs.existsSync(runtimeRoot) || !fs.statSync(runtimeRoot).isDirectory()) {
    fail(`stable root missing: ${runtimeRoot}`);
  }
  const entries = fs.readdirSync(runtimeRoot).sort();
  if (entries.length !== LAYOUT_ENTRIES.length || entries.some((entry, index) => entry !== LAYOUT_ENTRIES.slice().sort()[index])) {
    fail('flat runtime or legacy stable root layout rejected');
  }
  if (fs.existsSync(path.join(runtimeRoot, 'jax-rtc-sidecar.exe'))) {
    fail('flat runtime layout rejected');
  }
  return runtimeRoot;
}

function assertSafeChildPath(root, candidate, label) {
  const relative = path.relative(root, candidate);
  if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    fail(`invalid ${label} path`);
  }
  return relative.split(path.sep).join('/');
}

function walkPayloadFiles(root, current = root, result = {}, options = {}) {
  const entries = fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if (options.ignoreMetadata && current === root && entry.name === 'generation.json') continue;
    const target = path.join(current, entry.name);
    const relative = assertSafeChildPath(root, target, 'payload');
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink()) fail(`reparse or symbolic link rejected: ${relative}`);
    if (entry.isDirectory()) {
      walkPayloadFiles(root, target, result);
      continue;
    }
    if (!entry.isFile()) fail(`unsupported payload entry: ${relative}`);
    result[relative] = crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex');
  }
  return result;
}

function assertClosedPayload(stagingDir, expectedFiles) {
  if (!expectedFiles || typeof expectedFiles !== 'object' || Array.isArray(expectedFiles)) {
    fail('closed file set required');
  }
  const actualFiles = walkPayloadFiles(stagingDir);
  const expected = createGenerationMetadata({
    generation: generationIdForProvenance(fs.readFileSync(path.join(stagingDir, 'jax-rtc-sidecar.provenance.json'))),
    manifestSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(stagingDir, 'jax-rtc-sidecar.provenance.json'))).digest('hex'),
    files: expectedFiles,
  }).files;
  const actualKeys = Object.keys(actualFiles).sort();
  const expectedKeys = Object.keys(expected).sort();
  if (actualKeys.length !== expectedKeys.length || actualKeys.some((key, index) => key !== expectedKeys[index])) {
    fail('closed file set mismatch: extra or missing payload file');
  }
  for (const key of expectedKeys) {
    if (actualFiles[key] !== expected[key]) fail(`payload hash mismatch: ${key}`);
  }
  return actualFiles;
}

function verifyFinalizedGeneration({ runtimeDir, generation }) {
  if (typeof runtimeDir !== 'string' || runtimeDir.length === 0) fail('stable root is required');
  const parsedGeneration = parseGenerationId(generation);
  const generationDir = path.join(runtimeDir, 'generations', parsedGeneration);
  const metadataPath = path.join(generationDir, 'generation.json');
  if (!fs.existsSync(metadataPath)) fail('generation metadata missing');
  const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
  assertExactKeys(metadata, GENERATION_KEYS, 'generation metadata');
  const normalized = createGenerationMetadata({
    generation: metadata.generation,
    manifestSha256: metadata.manifest_sha256,
    files: metadata.files,
  });
  if (normalized.generation !== parsedGeneration) fail('generation metadata id mismatch');
  const actualFiles = walkPayloadFiles(generationDir, generationDir, {}, { ignoreMetadata: true });
  const actualKeys = Object.keys(actualFiles).sort();
  const expectedKeys = Object.keys(normalized.files).sort();
  if (actualKeys.length !== expectedKeys.length || actualKeys.some((key, index) => key !== expectedKeys[index])) {
    fail('finalized generation closed file set mismatch');
  }
  for (const key of expectedKeys) {
    if (actualFiles[key] !== normalized.files[key]) fail(`finalized payload hash mismatch: ${key}`);
  }
  return normalized;
}

function finalizeStagedGeneration({ runtimeDir, stagingDir, provenanceBytes, expectedFiles }) {
  if (typeof runtimeDir !== 'string' || runtimeDir.length === 0) fail('stable root is required');
  if (!Buffer.isBuffer(provenanceBytes) && !(provenanceBytes instanceof Uint8Array)) fail('provenance must be bytes');
  createRuntimeLayout(runtimeDir);
  const resolvedRoot = path.resolve(runtimeDir);
  const resolvedStaging = path.resolve(stagingDir);
  assertSafeChildPath(resolvedRoot, resolvedStaging, 'staging');
  if (!fs.existsSync(resolvedStaging) || !fs.lstatSync(resolvedStaging).isDirectory()) fail('staging directory required');
  const provenancePath = path.join(resolvedStaging, 'jax-rtc-sidecar.provenance.json');
  const actualProvenance = fs.readFileSync(provenancePath);
  if (!Buffer.from(actualProvenance).equals(Buffer.from(provenanceBytes))) fail('provenance bytes mismatch');
  const generation = generationIdForProvenance(provenanceBytes);
  const generationDir = path.join(resolvedRoot, 'generations', generation);
  if (fs.existsSync(generationDir)) fail(`immutable generation already exists: ${generation}`);
  const files = assertClosedPayload(resolvedStaging, expectedFiles);
  const metadata = createGenerationMetadata({
    generation,
    manifestSha256: crypto.createHash('sha256').update(provenanceBytes).digest('hex'),
    files,
  });
  fs.writeFileSync(path.join(resolvedStaging, 'generation.json'), `${JSON.stringify(metadata)}\n`, { flag: 'wx' });
  fs.renameSync(resolvedStaging, generationDir);
  return { generation, generationDir, metadata };
}

function replaceCurrentPointer(temporaryPath, currentPath, _options = {}) {
  if (typeof temporaryPath !== 'string' || typeof currentPath !== 'string') {
    fail('temporary and current pointer paths are required');
  }
  const temporaryAbsolute = path.resolve(temporaryPath);
  const currentAbsolute = path.resolve(currentPath);
  if (temporaryAbsolute === currentAbsolute) fail('temporary pointer must differ from current pointer');
  if (path.dirname(temporaryAbsolute) !== path.dirname(currentAbsolute)) {
    fail('temporary and current pointers must share a directory');
  }
  fs.renameSync(temporaryAbsolute, currentAbsolute);
  return currentAbsolute;
}

function writeAndSyncFile(filePath, bytes) {
  const descriptor = fs.openSync(filePath, 'wx');
  try {
    fs.writeSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function publishCurrentPointer(input) {
  if (!input || typeof input !== 'object') fail('current pointer publish input required');
  if (typeof input.runtimeDir !== 'string' || input.runtimeDir.length === 0) fail('stable root is required');
  const pointer = parseCurrentPointer(input.pointer);
  createRuntimeLayout(input.runtimeDir);
  const currentPath = path.join(input.runtimeDir, 'current.json');
  const temporaryPath = `${currentPath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  const bytes = Buffer.from(`${JSON.stringify(pointer)}\n`);
  try {
    writeAndSyncFile(temporaryPath, bytes);
    replaceCurrentPointer(temporaryPath, currentPath);
  } catch (error) {
    if (fs.existsSync(temporaryPath)) fs.rmSync(temporaryPath, { force: true });
    throw error;
  }
  return pointer;
}

// Task 1 boundary: later tasks add staging build/finalize, pointer publication, leases and GC.
function publishRuntime(input) {
  if (!input || typeof input !== 'object') fail('publish input required');
  if (typeof input.runtimeDir !== 'string' || input.runtimeDir.length === 0) fail('stable root is required');
  createRuntimeLayout(input.runtimeDir);
  const token = crypto.randomUUID();
  const stagingDir = path.join(input.runtimeDir, 'staging', `pending-${token}`);
  fs.mkdirSync(stagingDir, { recursive: true });
  return { runtimeRoot: input.runtimeDir, stagingDir, token };
}

module.exports = {
  assertStableRoot,
  createCurrentPointer,
  createGenerationMetadata,
  createRuntimeLayout,
  generationIdForProvenance,
  parseCurrentPointer,
  parseGenerationId,
  publishCurrentPointer,
  publishRuntime,
  replaceCurrentPointer,
  finalizeStagedGeneration,
  verifyFinalizedGeneration,
  // 供 lease/gc 上层复用的基础原语（不对外作为 pointer 协议公共 API）。
  assertExactKeys,
  fail,
  GENERATION_RE,
  writeAndSyncFile,
};
