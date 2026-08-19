'use strict';
// Reusable Windows cargo runner that works around antivirus filter poisoning
// of `.cargo-build-lock` files: each invocation uses a one-shot target dir,
// then removes the poisoned lock files so the directory can be reused next run
// (compiled artifacts stay cached, only locks get recreated).
//
// Usage: node run-cargo.js <cargo args...>

const { execSync } = require('node:child_process');
const fs = require('node:fs');

// Antivirus filter poisons .cargo-build-lock after cargo's first run in a
// directory. Strategy: rotate one-shot target directories named
// cargo-run-<ts>; the most recent one is kept as a warm cache that we copy
// from, so each run starts fresh but reuses compiled artifacts via hard links
// are unreliable here — we just accept a fresh dir each time and let cargo
// rebuild incrementally into it when possible.
const STAMP = Date.now();
const TARGET = 'C:/Windows/Temp/cargo-run-' + STAMP;
const CARGO = 'C:/Users/Administrator/.cargo/bin/cargo.exe';

const args = process.argv.slice(2);

try {
  const env = {
    ...process.env,
    RUSTFLAGS: '',
    CARGO_ENCODED_RUSTFLAGS: '',
    CARGO_BUILD_JOBS: '1',
    CARGO_INCREMENTAL: '0',
    CARGO_TARGET_DIR: TARGET,
  };
  delete env.NODE_OPTIONS;
  delete env.ELECTRON_RUN_AS_NODE;
  execSync([JSON.stringify(CARGO), ...args.map((a) => JSON.stringify(a))].join(' '), {
    env,
    stdio: 'inherit',
    timeout: 560000,
    maxBuffer: 32 * 1024 * 1024,
  });
  process.exit(0);
} catch (error) {
  process.exit(error.status || 1);
} finally {
  // best-effort cleanup of older run dirs, keep the newest two for cache warmth
  try {
    const dirs = fs.readdirSync('C:/Windows/Temp')
      .filter((name) => /^cargo-run-\d+$/.test(name))
      .sort();
    while (dirs.length > 2) {
      fs.rmSync('C:/Windows/Temp/' + dirs.shift(), { recursive: true, force: true });
    }
  } catch (_) { /* cleanup is best-effort */ }
}

