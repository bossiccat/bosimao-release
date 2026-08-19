'use strict';

// ADR-027 RP-01 / RP-02 确定性 crash barrier 子进程 publisher。
//
// 流程：build staging payload -> finalize immutable generation -> write temp pointer
// -> replace current pointer。每个持久屏障点用 IPC `{event:'barrier',point}` 通知父进程，
// 并等待父进程 `release` 消息；父进程可在任意屏障点强制 SIGKILL，实现确定性 crash
// （非 timing race）。
//
// `SIDECAR_LEGACY_SWAP=1` 时走 pre-ADR 的 delete-and-rename 交换，用于 RED 对照组：
// 证明测试 harness 确实能观测到 ENOENT gap（当前 atomic 实现则观测不到）。

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const {
  createCurrentPointer,
  createRuntimeLayout,
  finalizeStagedGeneration,
  publishCurrentPointer,
} = require('../lib/sidecar-runtime-publish');

function send(message) {
  if (typeof process.send === 'function') process.send(message);
}

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

// 先注册 listener 再发信号，避免父进程立即回 `release` 时丢失消息。
function barrier(name) {
  return new Promise((resolve) => {
    const handler = (message) => {
      if (message === 'release') {
        process.removeListener('message', handler);
        resolve();
      }
    };
    process.on('message', handler);
    send({ event: 'barrier', point: name });
  });
}

async function main() {
  const runtimeDir = process.env.SIDECAR_RUNTIME_DIR;
  const provenanceVersion = process.env.SIDECAR_PROVENANCE_VERSION || 'new';
  const legacySwap = process.env.SIDECAR_LEGACY_SWAP === '1';
  if (!runtimeDir) throw new Error('SIDECAR_RUNTIME_DIR is required');

  createRuntimeLayout(runtimeDir);

  // 1. 在 staging 构造完整 payload。
  const token = crypto.randomUUID();
  const stagingDir = path.join(runtimeDir, 'staging', `pending-${token}`);
  fs.mkdirSync(stagingDir, { recursive: true });
  const provenanceBytes = Buffer.from(JSON.stringify({ schema_version: 1, version: provenanceVersion }));
  const payload = {
    'jax-rtc-sidecar.exe': Buffer.from(`sidecar-binary-${provenanceVersion}`),
    'jax-rtc-sidecar.provenance.json': provenanceBytes,
    'resources/app/native/liteav.dll': Buffer.from(`liteav-${provenanceVersion}`),
  };
  const expectedFiles = {};
  for (const [relative, bytes] of Object.entries(payload)) {
    const target = path.join(stagingDir, ...relative.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes);
    expectedFiles[relative] = sha256(bytes);
  }
  await barrier('after-staging');

  // 2. finalize immutable generation。
  const { generation } = finalizeStagedGeneration({ runtimeDir, stagingDir, provenanceBytes, expectedFiles });
  await barrier('after-finalize');

  const pointer = createCurrentPointer({ generation, manifestSha256: sha256(provenanceBytes) });

  if (legacySwap) {
    // RED 对照：模拟 pre-ADR 的 unlink(current) + rename(temp, current) 交换。
    const currentPath = path.join(runtimeDir, 'current.json');
    const tempPath = `${currentPath}.${process.pid}.tmp`;
    fs.writeFileSync(tempPath, `${JSON.stringify(pointer)}\n`);
    await barrier('after-temp-write');
    fs.unlinkSync(currentPath); // <-- ENOENT gap：旧 pointer 已删除、新 pointer 未就位
    await barrier('legacy-gap');
    fs.renameSync(tempPath, currentPath);
    await barrier('after-replace');
  } else {
    publishCurrentPointer({ runtimeDir, pointer });
    await barrier('after-replace');
  }

  send({ event: 'done', generation });
  process.exit(0);
}

main().catch((error) => {
  send({ event: 'error', message: error && error.message ? error.message : String(error) });
  process.exit(1);
});
