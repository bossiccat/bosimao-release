'use strict';

// 干净检出回归：`pet-ui/src-tauri/binaries/` 被 .gitignore:171 整目录忽略，而协调锁
// 与 runtime 目录同级落在该目录内（见 lib/sidecar-runtime-coordination.js
// coordinationLockPath/:45）⇒ 新机器/新 CI 的第一次构建会在取锁时撞 ENOENT，并被报成
// SIDECAR_RUNTIME_COORDINATION_RUNTIME_PARENT_MISSING（真实失败：runs 35511160822 /
// 35511430482 的 windows-popup-gate 第 3 步）。本文件把"干净检出"这件事做成夹具：
// 隔离根下让 binaries/ 真实地不存在。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  acquireRuntimeLease,
  coordinationLockPath,
} = require('../lib/sidecar-runtime-coordination');
const {
  diagnosticCode,
  main,
  packageConfig,
} = require('../build-sidecar-external-bin');

function tempIsoRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-runtime-parent-'));
}

// 等价于干净检出：binDir（binaries/）不存在，runtimeDir 与锁文件都在它下面。
function cleanCheckoutConfig() {
  const iso = tempIsoRoot();
  const binDir = path.join(iso, 'pet-ui', 'src-tauri', 'binaries');
  return {
    iso,
    binDir,
    config: {
      ...packageConfig(),
      binDir,
      runtimeDir: path.join(binDir, 'jax-rtc-sidecar-runtime'),
    },
  };
}

// 旁路 cargo 与 electron 打包：本文件只关心"取租约之前能不能自建输出目录"。
// acquireRuntimeLease 一律不注入 —— 注入它就测不到根因。
function buildInput(config, overrides = {}) {
  return {
    args: [],
    packageConfig: () => config,
    cargoPath: process.execPath,
    helperPath: process.execPath,
    execute: () => ({ status: 0 }),
    buildPackage: () => ({
      external_bin: { build_input_file: 'iso.exe', sha256: '0'.repeat(64) },
    }),
    trustGate: () => {},
    ...overrides,
  };
}

function captureStdout(run) {
  const original = process.stdout.write;
  const chunks = [];
  process.stdout.write = (chunk) => {
    chunks.push(String(chunk));
    return true;
  };
  try {
    run();
  } finally {
    process.stdout.write = original;
  }
  return chunks.join('');
}

test('干净检出上构建入口自建 runtime 父目录并继续（不再 RUNTIME_PARENT_MISSING）', () => {
  const { iso, binDir, config } = cleanCheckoutConfig();
  const lockFile = coordinationLockPath(config.runtimeDir);
  try {
    assert.equal(fs.existsSync(binDir), false, '夹具前提：干净检出上 binaries/ 不存在');
    const stdout = captureStdout(() => main(buildInput(config)));
    assert.equal(stdout, `built iso.exe ${'0'.repeat(64)}\n`);
    assert.equal(fs.existsSync(binDir), true, '构建入口必须先把输出目录建出来');
    assert.equal(fs.existsSync(lockFile), false, '租约正常释放，不留残锁');
  } finally {
    fs.rmSync(iso, { recursive: true, force: true });
  }
});

test('重复构建幂等：目录已存在时再跑一次仍然成功', () => {
  const { iso, binDir, config } = cleanCheckoutConfig();
  const lockFile = coordinationLockPath(config.runtimeDir);
  try {
    captureStdout(() => main(buildInput(config)));
    const second = captureStdout(() => main(buildInput(config)));
    assert.equal(second, `built iso.exe ${'0'.repeat(64)}\n`);
    assert.equal(fs.existsSync(binDir), true);
    assert.equal(fs.existsSync(lockFile), false);
  } finally {
    fs.rmSync(iso, { recursive: true, force: true });
  }
});

test('建目录不削弱互斥：已持有租约时并发 publisher 仍然 BUSY', () => {
  const { iso, binDir, config } = cleanCheckoutConfig();
  const lockFile = coordinationLockPath(config.runtimeDir);
  const instant = '2026-09-20T09:00:00.000Z';
  let release;
  try {
    fs.mkdirSync(binDir, { recursive: true });
    release = acquireRuntimeLease(config.runtimeDir, {
      operation: 'publish',
      processCreationTime: instant,
      createdAt: instant,
      inspectProcess: () => ({ status: 'alive', pid: process.pid, creationTime: instant }),
    });
    assert.equal(fs.existsSync(lockFile), true, '夹具前提：第一次取租约成功');
    assert.throws(
      () => main(buildInput(config, {
        coordination: {
          inspectProcess: () => ({ status: 'alive', pid: process.pid, creationTime: instant }),
        },
      })),
      (error) => error.code === 'SIDECAR_RUNTIME_COORDINATION_BUSY',
    );
    assert.equal(fs.existsSync(lockFile), true, '被拒的第二次尝试不得动到既有 owner');
  } finally {
    if (release) release();
    fs.rmSync(iso, { recursive: true, force: true });
  }
});

test('父目录创建失败时响亮失败，且诊断说的是创建失败而不是父目录缺失', () => {
  const iso = tempIsoRoot();
  try {
    // 父目录位置上放一个普通文件：recursive mkdir 无法把它变成目录。
    const blocker = path.join(iso, 'blocker');
    fs.writeFileSync(blocker, 'not a directory');
    const config = {
      ...packageConfig(),
      binDir: blocker,
      runtimeDir: path.join(blocker, 'jax-rtc-sidecar-runtime'),
    };
    assert.throws(
      () => main(buildInput(config)),
      (error) => {
        assert.equal(error.code, 'SIDECAR_RUNTIME_PARENT_CREATE_FAILED');
        // Windows 上 Node 的 recursive mkdir 撞同名文件报 EEXIST，POSIX 报 ENOTDIR。
        assert.ok(
          ['EEXIST', 'ENOTDIR'].includes(error.last_errno_code),
          `unexpected errno ${error.last_errno_code}`,
        );
        assert.equal(error.target, blocker);
        assert.equal(diagnosticCode(error), 'SIDECAR_RUNTIME_PARENT_CREATE_FAILED');
        return true;
      },
    );
  } finally {
    fs.rmSync(iso, { recursive: true, force: true });
  }
});
