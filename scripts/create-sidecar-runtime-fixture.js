'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {
  createProvenance,
  sha256File,
  verifyPackage,
} = require('./lib/sidecar-package');

const root = path.resolve(__dirname, '..');
const sidecarDir = path.join(root, 'sidecar');
const binDir = path.join(root, 'pet-ui', 'src-tauri', 'binaries');
const runtimeDir = path.join(binDir, 'jax-rtc-sidecar-runtime');
const executable = path.join(binDir, 'jax-rtc-sidecar-x86_64-pc-windows-msvc.exe');
const sourceLockFile = path.join(sidecarDir, 'package-lock.json');
const hashFile = path.join(runtimeDir, 'jax-rtc-sidecar.exe.sha256');
const manifestFile = path.join(runtimeDir, 'jax-rtc-sidecar.provenance.json');
const manifestDigestFile = path.join(runtimeDir, 'jax-rtc-sidecar.provenance.sha256');
const versions = JSON.parse(fs.readFileSync(path.join(sidecarDir, 'package.json'), 'utf8'));
const config = {
  sidecarDir,
  binDir,
  runtimeDir,
  executable,
  hashFile,
  manifestFile,
  manifestDigestFile,
  installedFile: 'jax-rtc-sidecar.exe',
  sourceLockFile,
  sourceLockHash: sha256File(sourceLockFile),
  electronVersion: versions.devDependencies.electron,
  sdkVersion: versions.dependencies['trtc-electron-sdk'],
};

function write(relative, content) {
  const file = path.join(runtimeDir, relative);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

fs.rmSync(runtimeDir, { recursive: true, force: true });
fs.rmSync(executable, { force: true });
fs.mkdirSync(runtimeDir, { recursive: true });
fs.writeFileSync(executable, Buffer.from('test-only-sidecar-executable'));
for (const [file, content] of [
  ['ffmpeg.dll', 'ffmpeg'], ['resources.pak', 'pak'], ['icudtl.dat', 'icu'],
  ['v8_context_snapshot.bin', 'snapshot'], ['locales/en-US.pak', 'locale'],
]) write(file, content);
const sdkRoot = 'resources/app/node_modules/trtc-electron-sdk';
write(`${sdkRoot}/package.json`, `${JSON.stringify({ version: config.sdkVersion })}\n`);
for (const name of [
  'trtc_electron_sdk.node', 'liteav.dll', 'txffmpeg.dll',
  'txsoundtouch.dll', 'liteav_media_server.exe',
]) write(`${sdkRoot}/build/Release/${name}`, name);
const manifest = createProvenance(config);
fs.writeFileSync(hashFile, `${manifest.external_bin.sha256}\n`, 'ascii');
fs.writeFileSync(manifestFile, `${JSON.stringify(manifest, null, 2)}\n`);
fs.writeFileSync(manifestDigestFile, `${sha256File(manifestFile)}\n`, 'ascii');
verifyPackage(config);
process.stdout.write(`${crypto.createHash('sha256').update(fs.readFileSync(manifestFile)).digest('hex')}\n`);
