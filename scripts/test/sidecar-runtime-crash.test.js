'use strict';

// ADR-027 Task 8：RP-01 确定性 crash barrier + held-out 完整性 + immutable finalization
// + GC 删除失败保留重试。全部用真实子进程、真实磁盘与 IPC 屏障，拒绝 timing race。

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
  finalizeStagedGeneration,
  gcGenerations,
  parseCurrentPointer,
  publishCurrentPointer,
  verifyFinalizedGeneration,
} = require('../lib/sidecar-runtime-publish');

const { makeImmutableGeneration, restoreWritableGeneration } = require('../lib/sidecar-runtime-immutable');

const workerPath = path.join(__dirname, 'sidecar-runtime-crash-worker.js');

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function generationIdForVersion(version) {
  return `g-${sha256(Buffer.from(JSON.stringify({ schema_version: 1, version })))}`;
}

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-crash-'));
}

function setupRuntime() {
  const root = path.join(tempRoot(), 'runtime');
  createRuntimeLayout(root);
  return root;
}

// ---- 生产 fixture builder（RP-01 场景的 old/new generation 构造） ----

function buildGeneration(root, version) {
  const stagingDir = fs.mkdtempSync(path.join(root, 'staging', 'pending-'));
  const provenanceBytes = Buffer.from(JSON.stringify({ schema_version: 1, version }));
  const payload = {
    'jax-rtc-sidecar.exe': Buffer.from(`sidecar-binary-${version}`),
    'jax-rtc-sidecar.provenance.json': provenanceBytes,
    'resources/app/native/liteav.dll': Buffer.from(`liteav-${version}`),
  };
  const expectedFiles = {};
  for (const [relative, bytes] of Object.entries(payload)) {
    const target = path.join(stagingDir, ...relative.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes);
    expectedFiles[relative] = sha256(bytes);
  }
  const finalized = finalizeStagedGeneration({ runtimeDir: root, stagingDir, provenanceBytes, expectedFiles });
  return { generation: finalized.generation, generationDir: finalized.generationDir, provenanceBytes };
}

function setCurrent(root, generation) {
  const provenance = fs.readFileSync(path.join(root, 'generations', generation, 'jax-rtc-sidecar.provenance.json'));
  publishCurrentPointer({
    runtimeDir: root,
    pointer: createCurrentPointer({ generation, manifestSha256: sha256(provenance) }),
  });
}

// 完整 resolver：read pointer → parse → verifyFinalizedGeneration（打开并校验闭集）。
// 返回分类：ENOENT / MALFORMED / PARTIAL / COMPLETE:<generation>。
function resolveObservation(root) {
  const currentPath = path.join(root, 'current.json');
  let raw;
  try {
    raw = fs.readFileSync(currentPath, 'utf8');
  } catch (error) {
    if (error && error.code === 'ENOENT') return 'ENOENT';
    throw error;
  }
  let pointer;
  try {
    pointer = parseCurrentPointer(JSON.parse(raw));
  } catch {
    return 'MALFORMED';
  }
  try {
    verifyFinalizedGeneration({ runtimeDir: root, generation: pointer.generation });
  } catch {
    return 'PARTIAL';
  }
  return `COMPLETE:${pointer.generation}`;
}

// ---- 确定性 crash barrier publisher 控制 ----

function spawnCrashPublisher(root, { version = 'new', legacySwap = false } = {}) {
  const child = fork(workerPath, [], {
    env: {
      ...process.env,
      SIDECAR_RUNTIME_DIR: root,
      SIDECAR_PROVENANCE_VERSION: version,
      SIDECAR_LEGACY_SWAP: legacySwap ? '1' : '0',
    },
    stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
  });
  const barrierQueue = [];
  const waiterQueue = [];
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  child.on('message', (message) => {
    if (!message || typeof message !== 'object') return;
    if (message.event === 'barrier') {
      if (waiterQueue.length > 0) waiterQueue.shift()(message.point);
      else barrierQueue.push(message.point);
    } else if (message.event === 'error') {
      stderr += `\n${message.message}`;
    }
  });
  const exited = new Promise((resolve) => child.on('exit', (code, signal) => resolve({ code, signal })));
  return {
    child,
    exited,
    stderr: () => stderr,
    nextBarrier() {
      if (barrierQueue.length > 0) return Promise.resolve(barrierQueue.shift());
      return new Promise((resolve) => waiterQueue.push(resolve));
    },
    release() {
      child.send('release');
    },
    kill() {
      child.kill('SIGKILL');
    },
  };
}

// 依次消费并放行所有屏障，直到 `target`；返回时子进程暂停在 `target`（未被放行）。
async function driveToBarrier(publisher, target) {
  for (;;) {
    const point = await publisher.nextBarrier();
    if (point === target) return point;
    publisher.release();
  }
}

// ============================ RP-01 ============================

test('RP-01: reader loop never observes ENOENT/partial, crash at finalize leaves complete old', async () => {
  const root = setupRuntime();
  const old = buildGeneration(root, 'old');
  setCurrent(root, old.generation);

  const publisher = spawnCrashPublisher(root, { version: 'new' });
  await driveToBarrier(publisher, 'after-finalize'); // 新 generation 已 finalize，pointer 未换
  const observed = new Set();
  for (let i = 0; i < 50; i += 1) {
    observed.add(resolveObservation(root));
    await new Promise((resolve) => setImmediate(resolve));
  }
  publisher.kill(); // pointer replace 前 kill
  const result = await publisher.exited;

  assert.ok(result.signal === 'SIGKILL' || result.code !== 0, 'publisher must be killed');
  assert.deepEqual([...observed], [`COMPLETE:${old.generation}`]);
  assert.equal(resolveObservation(root), `COMPLETE:${old.generation}`);
});

test('RP-01: crash after pointer replace leaves only the complete new pointer', async () => {
  const root = setupRuntime();
  const old = buildGeneration(root, 'old');
  setCurrent(root, old.generation);
  const newGeneration = generationIdForVersion('new');

  const publisher = spawnCrashPublisher(root, { version: 'new' });
  await driveToBarrier(publisher, 'after-replace'); // replace 已完成
  publisher.kill();
  const result = await publisher.exited;

  assert.ok(result.signal === 'SIGKILL' || result.code !== 0, 'publisher must be killed');
  // 屏障保证 replace 已发生：reader 必观测完整 new，绝无 ENOENT/PARTIAL/MALFORMED。
  assert.equal(resolveObservation(root), `COMPLETE:${newGeneration}`);
});

test('RP-01 RED control: legacy delete-and-rename swap exposes ENOENT (harness sensitivity)', async () => {
  const root = setupRuntime();
  const old = buildGeneration(root, 'old');
  setCurrent(root, old.generation);

  const publisher = spawnCrashPublisher(root, { version: 'new', legacySwap: true });
  await driveToBarrier(publisher, 'legacy-gap'); // unlink(current) 已执行、rename 未执行
  const observed = new Set();
  for (let i = 0; i < 50; i += 1) {
    observed.add(resolveObservation(root));
    await new Promise((resolve) => setImmediate(resolve));
  }
  // RED 对照：legacy swap 在 gap 内必然暴露 ENOENT，证明 harness 足够敏感。
  assert.ok(observed.has('ENOENT'), `legacy swap must expose ENOENT, observed: ${[...observed]}`);

  publisher.release(); // 放行 legacy-gap → rename 完成
  await publisher.nextBarrier(); // after-replace
  publisher.release(); // 放行 after-replace → 子进程正常退出
  const result = await publisher.exited;
  assert.equal(result.code, 0, publisher.stderr());
});

// ============================ held-out 完整性（独立构造，不经 finalize） ============================

function writeHeldOutGeneration(root, version, { extraFiles = {} } = {}) {
  const provenance = Buffer.from(JSON.stringify({ schema_version: 1, version }));
  const generation = `g-${sha256(provenance)}`;
  const genDir = path.join(root, 'generations', generation);
  fs.mkdirSync(genDir, { recursive: true });
  const payload = {
    'jax-rtc-sidecar.exe': Buffer.from(`heldout-exe-${version}`),
    'jax-rtc-sidecar.provenance.json': provenance,
    'resources/app/native/liteav.dll': Buffer.from(`heldout-liteav-${version}`),
  };
  for (const [rel, bytes] of Object.entries(payload)) {
    const target = path.join(genDir, ...rel.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes);
  }
  for (const [rel, bytes] of Object.entries(extraFiles)) {
    const target = path.join(genDir, ...rel.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes);
  }
  const fileHashes = {};
  for (const [rel, bytes] of Object.entries(payload)) fileHashes[rel] = sha256(bytes);
  const metadata = {
    schema_version: 1,
    generation,
    manifest_sha256: sha256(provenance),
    files: fileHashes,
  };
  fs.writeFileSync(path.join(genDir, 'generation.json'), `${JSON.stringify(metadata)}\n`);
  fs.writeFileSync(path.join(root, 'current.json'), `${JSON.stringify({ schema_version: 1, generation, manifest_sha256: sha256(provenance) })}\n`);
  return { genDir, generation, provenance };
}

test('held-out: malformed generation.json is rejected fail-closed', () => {
  const root = setupRuntime();
  const { genDir } = writeHeldOutGeneration(root, 'malformed');
  fs.writeFileSync(path.join(genDir, 'generation.json'), '{"schema_version":1,"generation":');
  assert.equal(resolveObservation(root), 'PARTIAL');
});

test('held-out: reparse-point (symlink) payload is rejected fail-closed', () => {
  const root = setupRuntime();
  const outside = path.join(tempRoot(), 'outside.dll');
  fs.writeFileSync(outside, 'outside');
  const { genDir } = writeHeldOutGeneration(root, 'reparse');
  const link = path.join(genDir, 'resources', 'app', 'native', 'linked.dll');
  try {
    fs.symlinkSync(outside, link, 'file');
  } catch (error) {
    throw new Error(`symlink fixture failed: ${error.code || error.message}`, { cause: error });
  }
  assert.equal(resolveObservation(root), 'PARTIAL');
});

test('held-out: extra-file payload is rejected fail-closed', () => {
  const root = setupRuntime();
  writeHeldOutGeneration(root, 'extra', { extraFiles: { 'unexpected.dll': Buffer.from('extra') } });
  assert.equal(resolveObservation(root), 'PARTIAL');
});

test('held-out: pointer corruption (truncated JSON) is rejected fail-closed', () => {
  const root = setupRuntime();
  writeHeldOutGeneration(root, 'pointer-corrupt');
  fs.writeFileSync(path.join(root, 'current.json'), '{"schema_version":1,"generation":"g-');
  assert.equal(resolveObservation(root), 'MALFORMED');
});

test('held-out: path escape in generation.json files map is rejected fail-closed', () => {
  const root = setupRuntime();
  const { genDir, provenance } = writeHeldOutGeneration(root, 'escape');
  const metadata = {
    schema_version: 1,
    generation: `g-${sha256(provenance)}`,
    manifest_sha256: sha256(provenance),
    files: { '../escape.txt': 'a'.repeat(64) },
  };
  fs.writeFileSync(path.join(genDir, 'generation.json'), `${JSON.stringify(metadata)}\n`);
  assert.equal(resolveObservation(root), 'PARTIAL');
});

// ============================ immutable finalization ============================

test('finalize marks generation payload immutable: runtime write is rejected', () => {
  const root = setupRuntime();
  const { generationDir } = buildGeneration(root, 'immutable');
  const payload = path.join(generationDir, 'jax-rtc-sidecar.exe');
  assert.throws(
    () => fs.writeFileSync(payload, 'tampered'),
    (error) => error && (error.code === 'EPERM' || error.code === 'EACCES'),
  );
});

test('immutable generation remains readable for verification and pointer replacement', () => {
  const root = setupRuntime();
  const old = buildGeneration(root, 'readable-old');
  setCurrent(root, old.generation);
  assert.equal(resolveObservation(root), `COMPLETE:${old.generation}`);

  // pointer replacement 只写 generation 外的 current.json，不受只读 payload 影响。
  const next = buildGeneration(root, 'readable-new');
  setCurrent(root, next.generation);
  assert.equal(resolveObservation(root), `COMPLETE:${next.generation}`);
});

// ============================ GC 删除失败保留重试 ============================

test('GC deletion failure retains generation and retries on next run', async () => {
  const root = setupRuntime();
  const current = buildGeneration(root, 'gc-current');
  const target = buildGeneration(root, 'gc-target');
  setCurrent(root, current.generation);
  restoreWritableGeneration(target.generationDir);
  fs.utimesSync(target.generationDir, new Date(0), new Date(0));
  makeImmutableGeneration(target.generationDir);

  // 受控注入一次删除失败，避免依赖 OS 对当前工作目录删除的不同语义。
  let deleteAttempts = 0;
  const failOnce = (generationDir) => {
    deleteAttempts += 1;
    assert.equal(generationDir, target.generationDir);
    const error = new Error('injected busy generation directory');
    error.code = 'EBUSY';
    throw error;
  };
  const result = await gcGenerations({
    runtimeDir: root,
    retainCount: 0,
    minAgeMs: 0,
    processIdentityResolver: () => null,
    deleteGeneration: failOnce,
  });
  assert.equal(deleteAttempts, 1, 'controlled delete path must run exactly once');
  assert.equal(result.removed.includes(target.generation), false, 'deletion failure must not report removal');
  assert.equal(fs.existsSync(target.generationDir), true, 'generation retained on deletion failure');
  assert.throws(
    () => fs.writeFileSync(path.join(target.generationDir, 'jax-rtc-sidecar.exe'), 'tampered'),
    (error) => error && (error.code === 'EPERM' || error.code === 'EACCES'),
    'a retained generation must be re-frozen after a failed GC deletion',
  );

  const retried = await gcGenerations({ runtimeDir: root, retainCount: 0, minAgeMs: 0, processIdentityResolver: () => null });
  assert.equal(retried.removed.includes(target.generation), true, 'retry after failure removes generation');
  assert.equal(fs.existsSync(target.generationDir), false);
});
