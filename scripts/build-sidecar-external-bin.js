'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const {
  TARGET_TRIPLE,
  PackageError,
  buildPackage,
  verifyPackage,
} = require('./lib/sidecar-package');
const { assertProductionTrust } = require('./lib/sidecar-trust');

const root = path.resolve(__dirname, '..');
const sidecarDir = path.join(root, 'sidecar');
const binDir = path.join(root, 'pet-ui', 'src-tauri', 'binaries');
const runtimeDir = path.join(binDir, 'jax-rtc-sidecar-runtime');
const sourceLockFile = path.join(sidecarDir, 'package-lock.json');

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function lockedVersions() {
  const packageJson = JSON.parse(fs.readFileSync(path.join(sidecarDir, 'package.json'), 'utf8'));
  return {
    electronVersion: packageJson.devDependencies.electron,
    sdkVersion: packageJson.dependencies['trtc-electron-sdk'],
  };
}

function packageConfig() {
  const versions = lockedVersions();
  return {
    sidecarDir,
    binDir,
    runtimeDir,
    executable: path.join(binDir, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`),
    hashFile: path.join(runtimeDir, 'jax-rtc-sidecar.exe.sha256'),
    manifestFile: path.join(runtimeDir, 'jax-rtc-sidecar.provenance.json'),
    manifestDigestFile: path.join(runtimeDir, 'jax-rtc-sidecar.provenance.sha256'),
    installedFile: 'jax-rtc-sidecar.exe',
    sourceLockFile,
    sourceLockHash: sha256(sourceLockFile),
    ...versions,
  };
}

function main() {
  const verifyOnly = process.argv.slice(2).includes('--verify-only');
  const config = packageConfig();
  const manifest = verifyOnly ? verifyPackage(config) : buildPackage(config);
  assertProductionTrust({
    executable: config.executable,
    nativeDir: path.join(config.runtimeDir, 'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release'),
    runtimeDir: config.runtimeDir,
  });
  process.stdout.write(
    `${verifyOnly ? 'verified' : 'built'} ${manifest.external_bin.build_input_file} ${manifest.external_bin.sha256}\n`
  );
}

try {
  main();
} catch (error) {
  const code = error instanceof PackageError ? error.code : 'SIDECAR_PACKAGE_UNEXPECTED_FAILURE';
  process.stderr.write(`${code}\n`);
  process.exitCode = 1;
}
