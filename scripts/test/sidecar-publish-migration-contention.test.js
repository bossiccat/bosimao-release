'use strict';

const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

// 端到端争用证据：一个真实进程持有 runtime 租约时，另一个进程执行真实迁移 CLI
// 必须以 SIDECAR_RUNTIME_COORDINATION_BUSY 失败，且不创建 backup、不移动任何文件。
// 这不是注入接缝的单元测试——它走真实 CLI 入口与真实文件系统锁。
//
// 注意：本测试用文件系统证据（owner 文件出现/锁目录消失）做同步，不用子进程
// stdout 事件——主线程用 Atomics.wait 轮询时事件循环被阻塞，stdout 回调不会触发。

const root = path.resolve(__dirname, '..', '..');
const cliPath = path.join(root, 'scripts', 'build-sidecar-external-bin.js');
const runtimeDir = path.join(root, 'pet-ui', 'src-tauri', 'binaries', 'jax-rtc-sidecar-runtime');
// 单文件锁：锁文件本身即 owner 记录。
const lockFile = `${runtimeDir}.coordination-lock`;

const holderScript = `
const fs = require('fs');
const { acquireRuntimeLease } = require(${JSON.stringify(path.join(root, 'scripts', 'lib', 'sidecar-runtime-coordination.js'))});
try {
  const release = acquireRuntimeLease(process.env.JAX_LOCK_RUNTIME);
  process.stdout.write('HELD\\n');
  const flag = process.env.JAX_RELEASE_FLAG;
  const slice = new Int32Array(new SharedArrayBuffer(4));
  while (!fs.existsSync(flag)) Atomics.wait(slice, 0, 0, 200);
  release();
  process.stdout.write('RELEASED\\n');
} catch (error) {
  process.stderr.write('HOLDER_ERROR=' + (error.code || error.message) + '\\n');
  process.exit(3);
}
`;

function waitFor(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    const slice = new Int32Array(new SharedArrayBuffer(4));
    Atomics.wait(slice, 0, 0, 100);
  }
  throw new Error(`timed out waiting for ${label}`);
}

function removeTree(target) {
  if (!fs.existsSync(target)) return;
  assert.equal(target.includes("'"), false, 'cleanup paths must not contain quotes');
  try {
    fs.rmSync(target, { recursive: true, force: true });
    if (!fs.existsSync(target)) return;
  } catch {
    // fall through to the direct remover
  }
  spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', `Remove-Item -LiteralPath '${target}' -Recurse -Force`],
    { encoding: 'utf8', windowsHide: true, timeout: 60000 },
  );
}

test('a real migration CLI run is blocked while another process holds the lease', {
  skip: process.platform !== 'win32' ? 'windows-only end-to-end contention evidence' : false,
}, () => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-contention-'));
  const releaseFlag = path.join(scratch, 'release.flag');
  const backupDir = path.join(scratch, 'backup');
  // 隔离 worktree 可能没有 binaries 目录；租约锁是 runtime root 的兄弟节点，
  // 父目录必须存在才能建立锁。测试自建并在收尾清理，不遗留空壳目录。
  const runtimeDirExisted = fs.existsSync(runtimeDir);
  fs.mkdirSync(runtimeDir, { recursive: true });

  const holder = spawn(process.execPath, ['-e', holderScript], {
    env: {
      ...process.env,
      JAX_LOCK_RUNTIME: runtimeDir,
      JAX_RELEASE_FLAG: releaseFlag,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let holderStderr = '';
  holder.stderr.on('data', (chunk) => { holderStderr += String(chunk); });

  try {
    waitFor(
      () => fs.existsSync(lockFile),
      15000,
      'the holder to own the lease (lock file with owner record must appear)',
    );

    const contender = spawnSync(
      process.execPath,
      [cliPath, '--migrate-legacy-runtime', '--backup-dir', backupDir],
      { encoding: 'utf8', windowsHide: true, timeout: 120000 },
    );

    assert.equal(contender.status, 1, `contender must fail: stdout=${contender.stdout} stderr=${contender.stderr}`);
    assert.match(
      `${contender.stderr}\n${contender.stdout}`,
      /SIDECAR_RUNTIME_COORDINATION_BUSY/,
    );
    assert.equal(fs.existsSync(backupDir), false, 'no backup may be created while the lease is held');
    assert.equal(
      fs.existsSync(path.join(runtimeDir, 'current.json')),
      false,
      'the runtime must not be converted while the lease is held',
    );
  } finally {
    fs.writeFileSync(releaseFlag, '1');
    try {
      waitFor(() => !fs.existsSync(lockFile), 15000, 'the holder to release the lease');
    } catch (releaseError) {
      let ownerDump = 'OWNER_MISSING';
      try { ownerDump = fs.readFileSync(lockFile, 'utf8').trim(); } catch { /* already gone */ }
      throw new Error(
        `${releaseError.message}; holderExit=${holder.exitCode} holderKilled=${holder.killed}`
        + ` holderStderr=${JSON.stringify(holderStderr)} flagExists=${fs.existsSync(releaseFlag)}`
        + ` owner=${JSON.stringify(ownerDump)}`,
      );
    } finally {
      // 若 holder 被杀或异常退出，锁目录会残留 stale owner；
      // 测试收尾必须清掉，否则后续运行会被自己的残留锁阻断。
      removeTree(lockFile);
      if (!runtimeDirExisted) {
        removeTree(runtimeDir);
      }
      void holderStderr;
    }
  }
});
