'use strict';

// Electron 43 stdin 双回归修复的端到端验证（e2e 实锤 2026-10-05）：
//   回归 #1：E43 主进程 stdin 'end' 立即假触发 → 启动 2-4s 秒退；
//   回归 #2：stdin 'data' 永不触发 → shutdown 行死信。
// 修复：优雅停机迁移为 shutdown 文件信号（JAX_SIDECAR_SHUTDOWN_FILE）。
// 本套件证明：armed 后进程存活（#1 已绝）、写文件后受控退出 rc=0（#2 替代通）。

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const SIDECAR = path.resolve(__dirname, '..');
const ELECTRON = path.join(SIDECAR, 'node_modules', 'electron', 'dist', 'electron.exe');
const FIXTURE = path.join(__dirname, 'shutdown-file-fixture.js');
const READY_MARK = '[fixture] shutdown-watch armed';

function uniqueShutdownPath() {
  return path.join(
    os.tmpdir(),
    `jax-sidecar-test-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}.shutdown`
  );
}

function spawnFixture(withShutdownEnv) {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.NODE_OPTIONS;
  let shutdownPath = null;
  if (withShutdownEnv) {
    shutdownPath = uniqueShutdownPath();
    env.JAX_SIDECAR_SHUTDOWN_FILE = shutdownPath;
  } else {
    delete env.JAX_SIDECAR_SHUTDOWN_FILE;
  }
  const child = spawn(ELECTRON, ['--no-sandbox', FIXTURE], {
    cwd: SIDECAR,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return { child, shutdownPath };
}

// 等 stderr 就绪标记；提前退出则 reject（带退出码便于归因）。
function waitArmed(child) {
  return new Promise((resolve, reject) => {
    let stderr = '';
    const onData = (chunk) => {
      stderr += chunk;
      if (stderr.includes(READY_MARK)) {
        child.stderr.off('data', onData);
        resolve();
      }
    };
    child.stderr.on('data', onData);
    child.on('exit', (code) => reject(new Error(`fixture 提前退出 code=${code} stderr=${stderr}`)));
  });
}

// 先注册 exit 监听再等待（事件不回放，晚注册 = 永久假超时）。
function exitPromise(child) {
  return new Promise((resolve) => child.on('exit', (code) => resolve(code)));
}

function failAfter(ms, label, child) {
  return new Promise((_, reject) => {
    setTimeout(() => {
      child.kill();
      reject(new Error(`${label}（${ms}ms 超时，已强杀）`));
    }, ms);
  });
}

test('回归 #1 守卫：armed 后 5s 存活（E43 stdin 假 EOF 秒退已绝），随后文件停机 rc=0', async () => {
  const { child, shutdownPath } = spawnFixture(true);
  const exited = exitPromise(child);
  await waitArmed(child);
  await new Promise((r) => setTimeout(r, 5000));
  let verdict = await Promise.race([exited.then(() => 'exited'), Promise.resolve('alive')]);
  assert.equal(verdict, 'alive', '5s 观察窗口内不得自行退出（旧 stdin 假 EOF 在 2-4s 内秒退）');
  fs.writeFileSync(shutdownPath, '');
  const code = await Promise.race([exited, failAfter(15000, '写 shutdown 文件后 15s 未退出', child)]);
  assert.equal(code, 0, 'shutdown 文件必须触发受控退出 rc=0');
});

test('回归 #2 替代通：启动后写 shutdown 文件 → 15s 内受控退出 rc=0', async () => {
  const { child, shutdownPath } = spawnFixture(true);
  const exited = exitPromise(child);
  await waitArmed(child);
  fs.writeFileSync(shutdownPath, '');
  const code = await Promise.race([exited, failAfter(15000, '写 shutdown 文件后 15s 未退出', child)]);
  assert.equal(code, 0, 'shutdown 文件必须触发受控退出 rc=0');
});

test('env 未设不启用轮询：armed 后 1.5s 不自行退出（开发态直跑兼容）', async () => {
  const { child } = spawnFixture(false);
  const exited = exitPromise(child);
  await waitArmed(child);
  await new Promise((r) => setTimeout(r, 1500));
  const verdict = await Promise.race([exited.then(() => 'exited'), Promise.resolve('alive')]);
  assert.equal(verdict, 'alive', 'env 未设时不得有任何退出触发');
  child.kill();
  await exited; // 收尾清理，不判码（Windows kill 为 TerminateProcess）
});
