'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {
  ELECTRON_REQUIRED,
  GENERATION_METADATA_FILE,
  HASH_RE,
  NATIVE_REQUIRED,
  PROVENANCE_DIGEST_FILE,
  PROVENANCE_FILE,
  SHA_FILE,
  TARGET_TRIPLE,
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
} = require('./sidecar-package-common');

// package verifier：从 current.json 解析 pointer → 定位 generations/g-<id> →
// verifyFinalizedGeneration 闭集校验 → provenance 身份/版本/native 子集/闭集自洽。
// 无 flat fallback；target-triple 命名不得替代 installed jax-rtc-sidecar.exe 身份。
function verifyPackage(config) {
  if (!fs.existsSync(config.executable)) fail('SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');

  // 1. pointer → selected immutable generation → 闭集校验（无 flat fallback）。
  const { generationDir, pointer } = resolveCurrentGeneration(config);
  verifySelectedGeneration(config);

  // 2. 源码锁自洽。
  if (!HASH_RE.test(config.sourceLockHash) || sha256File(config.sourceLockFile) !== config.sourceLockHash) {
    fail('SIDECAR_PACKAGE_LOCK_MISMATCH');
  }

  // 3. provenance digest 自洽。
  const digestFile = path.join(generationDir, PROVENANCE_DIGEST_FILE);
  if (!fs.existsSync(digestFile)) fail('SIDECAR_PACKAGE_MANIFEST_DIGEST_MISSING');
  const expectedManifestDigest = fs.readFileSync(digestFile, 'utf8').trim();
  if (!HASH_RE.test(expectedManifestDigest)) fail('SIDECAR_PACKAGE_MANIFEST_DIGEST_INVALID');
  if (sha256File(path.join(generationDir, PROVENANCE_FILE)) !== expectedManifestDigest) {
    fail('SIDECAR_PACKAGE_MANIFEST_DIGEST_MISMATCH');
  }

  // 4. installed 身份二进制哈希自洽（target-triple 命名不得替代 jax-rtc-sidecar.exe）。
  const installedBin = path.join(generationDir, config.installedFile);
  if (!fs.existsSync(installedBin)) fail('SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
  const shaFile = path.join(generationDir, SHA_FILE);
  if (!fs.existsSync(shaFile)) fail('SIDECAR_PACKAGE_HASH_MISSING');
  const expectedHash = fs.readFileSync(shaFile, 'utf8').trim();
  if (!HASH_RE.test(expectedHash)) fail('SIDECAR_PACKAGE_HASH_INVALID');
  if (sha256File(installedBin) !== expectedHash) fail('SIDECAR_PACKAGE_HASH_MISMATCH');

  // 5. provenance schema + 版本 + externalBin 身份。
  const manifest = parseGenerationManifest(generationDir);
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

  // 6. generation 内 SDK 与 Electron/native 必需文件。
  const sdkPackage = path.join(sdkRoot(generationDir), 'package.json');
  if (!fs.existsSync(sdkPackage)) fail('SIDECAR_PACKAGE_SDK_MISSING');
  if (JSON.parse(fs.readFileSync(sdkPackage, 'utf8')).version !== config.sdkVersion) {
    fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
  }
  for (const name of NATIVE_REQUIRED) {
    if (!fs.existsSync(path.join(sdkRoot(generationDir), 'build', 'Release', name))) fail('SIDECAR_PACKAGE_NATIVE_MISSING');
  }
  for (const relative of ELECTRON_REQUIRED) {
    if (!fs.existsSync(path.join(generationDir, relative))) fail('SIDECAR_PACKAGE_RUNTIME_MISSING');
  }
  if (fs.existsSync(path.join(generationDir, 'resources', 'app', 'node_modules', 'electron'))) {
    fail('SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
  }

  // 7. runtime_files 闭集（排除 metadata 文件与 generation.json，与 Rust list_runtime_files 一致）。
  const excluded = new Set([...metadataFileSet(), GENERATION_METADATA_FILE]);
  const actualRuntimePaths = new Set(listFiles(generationDir).filter((item) => !excluded.has(item)));
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
    const file = path.join(generationDir, item.path);
    if (!fs.existsSync(file) || sha256File(file) !== item.sha256) fail('SIDECAR_PACKAGE_RUNTIME_MISMATCH');
  }

  // 8. pointer 的 manifest_sha256 必须等于 provenance 字节摘要（协议一致性）。
  const provenanceSha = sha256File(path.join(generationDir, PROVENANCE_FILE));
  if (pointer.manifest_sha256 !== provenanceSha) fail('SIDECAR_PACKAGE_POINTER_MANIFEST_MISMATCH');

  return manifest;
}

module.exports = { verifyPackage };
