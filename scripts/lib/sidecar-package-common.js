'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {
  parseCurrentPointer,
  verifyFinalizedGeneration,
} = require('./sidecar-runtime-publish');

const SCRIPT_VERSION = '1.0.0';
const TARGET_TRIPLE = 'x86_64-pc-windows-msvc';
const HASH_RE = /^[0-9a-f]{64}$/;
const INSTALLED_BIN = 'jax-rtc-sidecar.exe';
const SHA_FILE = 'jax-rtc-sidecar.exe.sha256';
const PROVENANCE_FILE = 'jax-rtc-sidecar.provenance.json';
const PROVENANCE_DIGEST_FILE = 'jax-rtc-sidecar.provenance.sha256';
const GENERATION_METADATA_FILE = 'generation.json';
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

function sdkRoot(contentRoot) {
  return path.join(contentRoot, 'resources', 'app', 'node_modules', 'trtc-electron-sdk');
}

// provenance 清单的 metadata 文件相对名（flat 假设已移除，只按名字排除，不按根目录路径）。
function metadataFileSet() {
  return new Set([SHA_FILE, PROVENANCE_FILE, PROVENANCE_DIGEST_FILE]);
}

// 从 contentRoot（staging 或 generation 目录）构造 provenance manifest。
// contentRoot 语义：stable root 之下的 staging 或不可变 generation 目录，禁止 flat runtimeDir。
function createProvenance(config, contentRoot) {
  if (!contentRoot || typeof contentRoot !== 'string') fail('SIDECAR_PACKAGE_CONTENT_ROOT_REQUIRED');
  if (!fs.existsSync(config.executable)) fail('SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
  const installedBin = path.join(contentRoot, config.installedFile);
  if (!fs.existsSync(installedBin)) fail('SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
  const sdk = sdkRoot(contentRoot);
  const sdkPackage = path.join(sdk, 'package.json');
  if (!fs.existsSync(sdkPackage)) fail('SIDECAR_PACKAGE_SDK_MISSING');
  const installedVersion = JSON.parse(fs.readFileSync(sdkPackage, 'utf8')).version;
  if (installedVersion !== config.sdkVersion) fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
  const nativeFiles = NATIVE_REQUIRED.map((name) => `resources/app/node_modules/trtc-electron-sdk/build/Release/${name}`);
  for (const relative of nativeFiles) {
    if (!fs.existsSync(path.join(contentRoot, relative))) fail('SIDECAR_PACKAGE_NATIVE_MISSING');
  }
  for (const relative of ELECTRON_REQUIRED) {
    if (!fs.existsSync(path.join(contentRoot, relative))) fail('SIDECAR_PACKAGE_RUNTIME_MISSING');
  }
  if (fs.existsSync(path.join(contentRoot, 'resources', 'app', 'node_modules', 'electron'))) {
    fail('SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
  }
  const runtimeFiles = listFiles(contentRoot).filter((item) => !metadataFileSet().has(item));
  return {
    schema_version: 1,
    build_script_version: SCRIPT_VERSION,
    target_triple: TARGET_TRIPLE,
    electron_version: config.electronVersion,
    trtc_sdk_version: installedVersion,
    sidecar_package_lock_sha256: config.sourceLockHash,
    external_bin: {
      build_input_file: path.basename(config.executable),
      installed_file: config.installedFile,
      target_triple: TARGET_TRIPLE,
      sha256: sha256File(installedBin),
    },
    native_files: hashFiles(contentRoot, nativeFiles),
    runtime_files: hashFiles(contentRoot, runtimeFiles),
    bundle_resources: expectedBundleResourceMap(),
  };
}

// 解析 stable root 的 current.json pointer，定位 selected immutable generation。
function resolveCurrentGeneration(config) {
  const pointerPath = path.join(config.runtimeDir, 'current.json');
  if (!fs.existsSync(pointerPath)) fail('SIDECAR_PACKAGE_POINTER_MISSING');
  let pointer;
  try {
    pointer = parseCurrentPointer(JSON.parse(fs.readFileSync(pointerPath, 'utf8')));
  } catch (_) {
    fail('SIDECAR_PACKAGE_POINTER_INVALID');
  }
  const generationDir = path.join(config.runtimeDir, 'generations', pointer.generation);
  if (!fs.existsSync(generationDir) || !fs.statSync(generationDir).isDirectory()) {
    fail('SIDECAR_PACKAGE_GENERATION_MISSING');
  }
  return { generation: pointer.generation, generationDir, pointer };
}

// 用 pointer 协议闭集校验 selected generation（generation.json + 闭集 payload）。
function verifySelectedGeneration(config) {
  const { generation } = resolveCurrentGeneration(config);
  try {
    return verifyFinalizedGeneration({ runtimeDir: config.runtimeDir, generation });
  } catch (error) {
    if (error instanceof PackageError) throw error;
    fail('SIDECAR_PACKAGE_GENERATION_INVALID');
  }
}

function parseGenerationManifest(generationDir) {
  const manifestPath = path.join(generationDir, PROVENANCE_FILE);
  if (!fs.existsSync(manifestPath)) fail('SIDECAR_PACKAGE_MANIFEST_MISSING');
  try {
    return JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch (_) {
    fail('SIDECAR_PACKAGE_MANIFEST_INVALID');
  }
}

function closedFileMap(root) {
  const map = {};
  for (const relative of listFiles(root)) map[relative] = sha256File(path.join(root, relative));
  return map;
}

module.exports = {
  APP_SOURCES,
  ELECTRON_REQUIRED,
  GENERATION_METADATA_FILE,
  HASH_RE,
  INSTALLED_BIN,
  NATIVE_REQUIRED,
  PROVENANCE_DIGEST_FILE,
  PROVENANCE_FILE,
  SCRIPT_VERSION,
  SHA_FILE,
  TARGET_TRIPLE,
  PackageError,
  closedFileMap,
  createProvenance,
  expectedBundleResourceMap,
  fail,
  listFiles,
  metadataFileSet,
  parseGenerationManifest,
  resolveCurrentGeneration,
  sdkRoot,
  sha256File,
  validateManifestSchema,
  verifyAppSourceSet,
  verifySelectedGeneration,
};
