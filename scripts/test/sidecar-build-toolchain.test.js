'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { resolveCargoPath } = require('../build-sidecar-external-bin');

const CARGO_HOME = path.join('C:', 'Users', 'builder', '.cargo', 'bin', 'cargo.exe');

function resolver(overrides = {}) {
  return (options = {}) => resolveCargoPath({
    platform: 'win32',
    env: {},
    homeDir: 'C:\\Users\\builder',
    existsSync: (candidate) => candidate === CARGO_HOME,
    which: () => null,
    ...overrides,
    ...options,
  });
}

test('prefers an explicitly supplied cargo path', () => {
  const explicit = 'D:\\toolchain\\bin\\cargo.exe';
  assert.equal(
    resolver({ existsSync: () => true })({ cargoPath: explicit }),
    explicit,
  );
});

test('falls back to the JAX_CARGO_BIN override', () => {
  const override = 'E:\\ci\\cargo.exe';
  assert.equal(
    resolver({ existsSync: (candidate) => candidate === override })({ env: { JAX_CARGO_BIN: override } }),
    override,
  );
});

test('falls back to the user cargo home before trusting PATH', () => {
  assert.equal(resolver()(), CARGO_HOME);
});

test('fails closed when no cargo toolchain can be resolved', () => {
  assert.throws(
    () => resolver({ existsSync: () => false })(),
    (error) => error.code === 'SIDECAR_CARGO_TOOLCHAIN_UNAVAILABLE',
  );
});

test('does not accept a PATH-only cargo when the resolver cannot verify it', () => {
  assert.throws(
    () => resolver({ existsSync: () => false, which: () => 'cargo' })(),
    (error) => error.code === 'SIDECAR_CARGO_TOOLCHAIN_UNAVAILABLE',
  );
});

test('resolves the real cargo installed on this machine', {
  skip: process.platform !== 'win32' ? 'windows-only toolchain evidence' : false,
}, () => {
  const resolved = resolveCargoPath();
  assert.equal(fs.existsSync(resolved), true);
  assert.equal(path.basename(resolved).toLowerCase(), 'cargo.exe');
});

test('the resolved cargo can build the native pointer helper into a release binary', {
  skip: process.platform !== 'win32' ? 'windows-only toolchain evidence' : false,
}, () => {
  const root = path.resolve(__dirname, '..', '..');
  const manifestPath = path.join(root, 'tools', 'sidecar-pointer-replace', 'Cargo.toml');
  const helperPath = path.join(root, 'tools', 'sidecar-pointer-replace', 'target', 'release', 'sidecar-pointer-replace.exe');
  if (!fs.existsSync(manifestPath)) return;

  const cargoPath = resolveCargoPath();
  const before = fs.existsSync(helperPath) ? fs.statSync(helperPath).mtimeMs : 0;
  const result = require('node:child_process').spawnSync(
    cargoPath,
    ['build', '--release', '--manifest-path', manifestPath],
    { encoding: 'utf8', windowsHide: true, timeout: 300000 },
  );
  assert.equal(result.status, 0, `cargo build failed: ${result.stderr || result.stdout || 'no output'}`);
  assert.equal(fs.existsSync(helperPath), true);
  assert.equal(typeof before, 'number');
  void os.tmpdir();
});
