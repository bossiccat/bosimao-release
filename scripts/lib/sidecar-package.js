'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { buildPackage: buildSidecarPackage } = require('./sidecar-package-build');

const SCRIPT_VERSION = '1.0.0';
const TARGET_TRIPLE = 'x86_64-pc-windows-msvc';
const HASH_RE = /^[0-9a-f]{64}$/;
const NATIVE_REQUIRED = [
  'trtc_electron_sdk.node',
  'liteav.dll',
  'txffmpeg.dll',
  'txsoundtouch.dll',
  'liteav_media_server.exe',
];
const ELECTRON_REQUIRED = [
  'ffmpeg.dll',
  'resources.pak',
  'icudtl.dat',
  'v8_context_snapshot.bin',
  'locales/en-US.pak',
];
const APP_SOURCES = [
  'audio.js', 'bridge.js', 'config.js', 'exit-protocol.js', 'index.html', 'logger.js',
  'main.js', 'phone.js', 'rtc-startup.js', 'rtc.js', 'security.js',
  'package.json', 'package-lock.json',
];
const MANIFEST_KEYS = [
  'schema_version', 'build_script_version', 'target_triple', 'electron_version',
  'trtc_sdk_version', 'sidecar_package_lock_sha256', 'external_bin', 'native_files',
  'runtime_files', 'bundle_resources',
];
const EXTERNAL_BIN_KEYS = ['build_input_file', 'installed_file', 'target_triple', 'sha256'];
const FILE_KEYS = ['path', 'sha256'];

class PackageError extends Error {
  constructor(code) {
    super(code);
    this.name = 'PackageError';
    this.code = code;
  }
}

function fail(code) {
  throw new PackageError(code);
}

function sha256File(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function normalize(relative) {
  return relative.split(path.sep).join('/');
}

function listFiles(root, current = root) {
  if (!fs.existsSync(current)) return [];
  const result = [];
  for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const item = path.join(current, entry.name);
    if (entry.isSymbolicLink()) fail('SIDECAR_PACKAGE_SYMLINK_FORBIDDEN');
    if (entry.isDirectory()) result.push(...listFiles(root, item));
    else if (entry.isFile()) result.push(normalize(path.relative(root, item)));
  }
  return result;
}

function hashFiles(root, files) {
  return files.map((relative) => ({ path: normalize(relative), sha256: sha256File(path.join(root, relative)) }));
}

function sameKeys(value, expected) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join('\0') === [...expected].sort().join('\0');
}

function validateRelativePath(relative) {
  if (typeof relative !== 'string' || relative.length === 0 || relative.includes('\\')) {
    fail('SIDECAR_PACKAGE_MANIFEST_PATH_INVALID');
  }
  const normalized = path.posix.normalize(relative);
  if (normalized !== relative || path.posix.isAbsolute(relative) || /^[A-Za-z]:\//.test(relative)
      || relative === '..' || relative.startsWith('../')) {
    fail('SIDECAR_PACKAGE_MANIFEST_PATH_INVALID');
  }
}

function validateFileEntries(entries) {
  if (!Array.isArray(entries)) fail('SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
  const paths = new Set();
  for (const item of entries) {
    if (!sameKeys(item, FILE_KEYS) || !HASH_RE.test(item.sha256)) {
      fail('SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
    }
    validateRelativePath(item.path);
    if (paths.has(item.path)) fail('SIDECAR_PACKAGE_MANIFEST_PATH_INVALID');
    paths.add(item.path);
  }
  return paths;
}

function validateManifestSchema(manifest) {
  if (!sameKeys(manifest, MANIFEST_KEYS) || manifest.schema_version !== 1
      || manifest.build_script_version !== SCRIPT_VERSION
      || manifest.target_triple !== TARGET_TRIPLE
      || !sameKeys(manifest.external_bin, EXTERNAL_BIN_KEYS)
      || !HASH_RE.test(manifest.external_bin.sha256)
      || !sameKeys(manifest.bundle_resources, Object.keys(expectedBundleResourceMap()))
      || manifest.bundle_resources['binaries/jax-rtc-sidecar-runtime/'] !== 'jax-rtc-sidecar-runtime/') {
    fail('SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
  }
  validateRelativePath(manifest.external_bin.build_input_file);
  validateRelativePath(manifest.external_bin.installed_file);
  return {
    nativePaths: validateFileEntries(manifest.native_files),
    runtimePaths: validateFileEntries(manifest.runtime_files),
  };
}

function verifyAppSourceSet(sidecarDir) {
  if (!sidecarDir) return;
  const actual = fs.readdirSync(sidecarDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && (entry.name.endsWith('.js') || entry.name === 'index.html'
      || entry.name === 'package.json' || entry.name === 'package-lock.json'))
    .map((entry) => entry.name)
    .sort();
  const expected = [...APP_SOURCES].sort();
  if (actual.join('\0') !== expected.join('\0')) fail('SIDECAR_PACKAGE_APP_SOURCE_SET_MISMATCH');
}

function expectedBundleResourceMap() {
  return { 'binaries/jax-rtc-sidecar-runtime/': 'jax-rtc-sidecar-runtime/' };
}

function sdkRoot(config) {
  return path.join(config.runtimeDir, 'resources', 'app', 'node_modules', 'trtc-electron-sdk');
}

function createProvenance(config) {
  if (!fs.existsSync(config.executable)) fail('SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
  const sdk = sdkRoot(config);
  const sdkPackage = path.join(sdk, 'package.json');
  if (!fs.existsSync(sdkPackage)) fail('SIDECAR_PACKAGE_SDK_MISSING');
  const installedVersion = JSON.parse(fs.readFileSync(sdkPackage, 'utf8')).version;
  if (installedVersion !== config.sdkVersion) fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
  const nativeFiles = NATIVE_REQUIRED.map((name) => `resources/app/node_modules/trtc-electron-sdk/build/Release/${name}`);
  for (const relative of nativeFiles) {
    if (!fs.existsSync(path.join(config.runtimeDir, relative))) fail('SIDECAR_PACKAGE_NATIVE_MISSING');
  }
  for (const relative of ELECTRON_REQUIRED) {
    if (!fs.existsSync(path.join(config.runtimeDir, relative))) fail('SIDECAR_PACKAGE_RUNTIME_MISSING');
  }
  if (fs.existsSync(path.join(config.runtimeDir, 'resources', 'app', 'node_modules', 'electron'))) {
    fail('SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
  }
  const excluded = new Set([
    normalize(path.relative(config.runtimeDir, config.hashFile)),
    normalize(path.relative(config.runtimeDir, config.manifestFile)),
    ...(config.manifestDigestFile
      ? [normalize(path.relative(config.runtimeDir, config.manifestDigestFile))]
      : []),
  ]);
  const runtimeFiles = listFiles(config.runtimeDir).filter((item) => !excluded.has(item));
  return {
    schema_version: 1,
    build_script_version: SCRIPT_VERSION,
    target_triple: TARGET_TRIPLE,
    electron_version: config.electronVersion,
    trtc_sdk_version: installedVersion,
    sidecar_package_lock_sha256: config.sourceLockHash,
    external_bin: {
      build_input_file: path.basename(config.executable),
      installed_file: config.installedFile || 'jax-rtc-sidecar.exe',
      target_triple: TARGET_TRIPLE,
      sha256: sha256File(config.executable),
    },
    native_files: hashFiles(config.runtimeDir, nativeFiles),
    runtime_files: hashFiles(config.runtimeDir, runtimeFiles),
    bundle_resources: expectedBundleResourceMap(),
  };
}

function parseManifest(config) {
  if (!fs.existsSync(config.manifestFile)) fail('SIDECAR_PACKAGE_MANIFEST_MISSING');
  try {
    return JSON.parse(fs.readFileSync(config.manifestFile, 'utf8'));
  } catch (_) {
    fail('SIDECAR_PACKAGE_MANIFEST_INVALID');
  }
}

function verifyPackage(config) {
  if (!fs.existsSync(config.executable)) fail('SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
  if (!fs.existsSync(config.hashFile)) fail('SIDECAR_PACKAGE_HASH_MISSING');
  const expectedHash = fs.readFileSync(config.hashFile, 'utf8').trim();
  if (!HASH_RE.test(expectedHash)) fail('SIDECAR_PACKAGE_HASH_INVALID');
  if (sha256File(config.executable) !== expectedHash) fail('SIDECAR_PACKAGE_HASH_MISMATCH');
  if (!HASH_RE.test(config.sourceLockHash) || sha256File(config.sourceLockFile) !== config.sourceLockHash) {
    fail('SIDECAR_PACKAGE_LOCK_MISMATCH');
  }
  const manifest = parseManifest(config);
  if (config.manifestDigestFile) {
    if (!fs.existsSync(config.manifestDigestFile)) fail('SIDECAR_PACKAGE_MANIFEST_DIGEST_MISSING');
    const expectedManifestDigest = fs.readFileSync(config.manifestDigestFile, 'utf8').trim();
    if (!HASH_RE.test(expectedManifestDigest)) fail('SIDECAR_PACKAGE_MANIFEST_DIGEST_INVALID');
    if (sha256File(config.manifestFile) !== expectedManifestDigest) {
      fail('SIDECAR_PACKAGE_MANIFEST_DIGEST_MISMATCH');
    }
  }
  const { nativePaths, runtimePaths } = validateManifestSchema(manifest);
  verifyAppSourceSet(config.sidecarDir);
  if (manifest.sidecar_package_lock_sha256 !== config.sourceLockHash) fail('SIDECAR_PACKAGE_LOCK_MISMATCH');
  if (manifest.electron_version !== config.electronVersion) fail('SIDECAR_PACKAGE_ELECTRON_VERSION_MISMATCH');
  if (manifest.trtc_sdk_version !== config.sdkVersion) fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
  if (manifest.external_bin.sha256 !== expectedHash) fail('SIDECAR_PACKAGE_MANIFEST_MISMATCH');
  if (manifest.external_bin.build_input_file !== path.basename(config.executable)
      || manifest.external_bin.installed_file !== config.installedFile
      || manifest.external_bin.target_triple !== TARGET_TRIPLE) {
    fail('SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH');
  }
  const sdkPackage = path.join(sdkRoot(config), 'package.json');
  if (!fs.existsSync(sdkPackage)) fail('SIDECAR_PACKAGE_SDK_MISSING');
  if (JSON.parse(fs.readFileSync(sdkPackage, 'utf8')).version !== config.sdkVersion) {
    fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
  }
  for (const name of NATIVE_REQUIRED) {
    if (!fs.existsSync(path.join(sdkRoot(config), 'build', 'Release', name))) fail('SIDECAR_PACKAGE_NATIVE_MISSING');
  }
  for (const relative of ELECTRON_REQUIRED) {
    if (!fs.existsSync(path.join(config.runtimeDir, relative))) fail('SIDECAR_PACKAGE_RUNTIME_MISSING');
  }
  if (fs.existsSync(path.join(config.runtimeDir, 'resources', 'app', 'node_modules', 'electron'))) {
    fail('SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
  }
  const excluded = new Set([
    normalize(path.relative(config.runtimeDir, config.hashFile)),
    normalize(path.relative(config.runtimeDir, config.manifestFile)),
    ...(config.manifestDigestFile
      ? [normalize(path.relative(config.runtimeDir, config.manifestDigestFile))]
      : []),
  ]);
  const actualRuntimePaths = new Set(listFiles(config.runtimeDir).filter((item) => !excluded.has(item)));
  if (actualRuntimePaths.size !== runtimePaths.size
      || [...actualRuntimePaths].some((item) => !runtimePaths.has(item))) {
    fail('SIDECAR_PACKAGE_RUNTIME_SET_MISMATCH');
  }
  const requiredNative = new Set(NATIVE_REQUIRED.map(
    (name) => `resources/app/node_modules/trtc-electron-sdk/build/Release/${name}`
  ));
  if (nativePaths.size !== requiredNative.size
      || [...requiredNative].some((item) => !nativePaths.has(item))) {
    fail('SIDECAR_PACKAGE_NATIVE_SET_MISMATCH');
  }
  const runtimeByPath = new Map(manifest.runtime_files.map((item) => [item.path, item.sha256]));
  for (const item of manifest.native_files) {
    if (runtimeByPath.get(item.path) !== item.sha256) fail('SIDECAR_PACKAGE_NATIVE_SUBSET_MISMATCH');
  }
  for (const item of manifest.runtime_files) {
    const file = path.join(config.runtimeDir, item.path);
    if (!fs.existsSync(file) || sha256File(file) !== item.sha256) fail('SIDECAR_PACKAGE_RUNTIME_MISMATCH');
  }
  return manifest;
}

function buildPackage(config) {
  return buildSidecarPackage(config, {
    APP_SOURCES,
    createProvenance,
    fail,
    sha256File,
    verifyAppSourceSet,
    verifyPackage,
  });
}

module.exports = {
  APP_SOURCES,
  HASH_RE,
  SCRIPT_VERSION,
  TARGET_TRIPLE,
  PackageError,
  buildPackage,
  createProvenance,
  expectedBundleResourceMap,
  sha256File,
  verifyPackage,
};
