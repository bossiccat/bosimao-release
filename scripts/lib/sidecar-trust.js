'use strict';

const fs = require('node:fs');
const path = require('node:path');

// Production trust policy for the Windows Electron/TRTC sidecar runtime.
// Distinguishes "test fixture self-consistency" (hash/set/version checks in
// sidecar-package.js) from "production runtime trust" (real binary size and
// PE provenance). Hash self-consistency alone never proves trustworthiness:
// a 28-byte externalBin and 10-23-byte native stubs can be internally
// consistent and still be useless as a commercial runtime.

const TRUST_VERSION = '1.0.0';

const NATIVE_NAMES = [
  'trtc_electron_sdk.node',
  'liteav.dll',
  'txffmpeg.dll',
  'txsoundtouch.dll',
  'liteav_media_server.exe',
];

// Conservative lower bounds far above any fixture stub and below any real
// Electron 31.7.7 / TRTC 13.4.802-beta.3 artifact observed on this machine.
const MIN_EXTERNAL_BIN_BYTES = 4 * 1024 * 1024; // real electron.exe ~172 MB
const MIN_NATIVE_BYTES = 32 * 1024;             // real native set >= 139 KB
const MIN_ELECTRON_BYTES = {
  'ffmpeg.dll': 512 * 1024,
  'resources.pak': 512 * 1024,
  'icudtl.dat': 512 * 1024,
  'v8_context_snapshot.bin': 64 * 1024,
  'locales/en-US.pak': 32 * 1024,
};

class TrustError extends Error {
  constructor(code) {
    super(code);
    this.name = 'TrustError';
    this.code = code;
  }
}

function fail(code) {
  throw new TrustError(code);
}

function isPeBinary(file) {
  try {
    const fd = fs.openSync(file, 'r');
    try {
      const head = Buffer.alloc(2);
      fs.readSync(fd, head, 0, 2, 0);
      return head[0] === 0x4d && head[1] === 0x5a;
    } finally {
      fs.closeSync(fd);
    }
  } catch (_) {
    return false;
  }
}

function assertProductionTrust(input) {
  if (!input || !input.executable || !input.nativeDir || !input.runtimeDir) {
    fail('SIDECAR_PACKAGE_TRUST_INPUT_INVALID');
  }
  const { executable, nativeDir, runtimeDir } = input;

  if (!fs.existsSync(executable)) fail('SIDECAR_PACKAGE_TRUST_MISSING');
  if (fs.statSync(executable).size < MIN_EXTERNAL_BIN_BYTES) fail('SIDECAR_PACKAGE_TRUST_MIN_SIZE');
  if (!isPeBinary(executable)) fail('SIDECAR_PACKAGE_TRUST_PE_HEADER');

  for (const name of NATIVE_NAMES) {
    const file = path.join(nativeDir, name);
    if (!fs.existsSync(file)) fail('SIDECAR_PACKAGE_TRUST_MISSING');
    if (fs.statSync(file).size < MIN_NATIVE_BYTES) fail('SIDECAR_PACKAGE_TRUST_MIN_SIZE');
    if (!isPeBinary(file)) fail('SIDECAR_PACKAGE_TRUST_PE_HEADER');
  }

  for (const [name, minBytes] of Object.entries(MIN_ELECTRON_BYTES)) {
    const file = path.join(runtimeDir, name);
    if (!fs.existsSync(file)) fail('SIDECAR_PACKAGE_TRUST_MISSING');
    if (fs.statSync(file).size < minBytes) fail('SIDECAR_PACKAGE_TRUST_MIN_SIZE');
  }
}

module.exports = {
  TRUST_VERSION,
  assertProductionTrust,
  isPeBinary,
  MIN_EXTERNAL_BIN_BYTES,
  MIN_NATIVE_BYTES,
  NATIVE_NAMES,
};
