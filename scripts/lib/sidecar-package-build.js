'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// ADR-027 generation 内的稳定 metadata 文件名（与 sidecar-package.js 常量一致）。
const SHA_FILE = 'jax-rtc-sidecar.exe.sha256';
const PROVENANCE_FILE = 'jax-rtc-sidecar.provenance.json';
const PROVENANCE_DIGEST_FILE = 'jax-rtc-sidecar.provenance.sha256';

function runNpm(args, cwd, fail) {
  const npmCli = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');
  const result = fs.existsSync(npmCli)
    ? spawnSync(process.execPath, [npmCli, ...args], { cwd, stdio: 'inherit', shell: false, env: { ...process.env } })
    : spawnSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', args, {
      cwd,
      stdio: 'inherit',
      shell: false,
      env: { ...process.env },
    });
  if (result.status !== 0) fail('SIDECAR_PACKAGE_NPM_CI_FAILED');
}

function buildPackage(config, api) {
  const {
    APP_SOURCES,
    createProvenance,
    fail,
    sha256File,
    verifyAppSourceSet,
    verifyPackage,
    createCurrentPointer,
    createRuntimeLayout,
    finalizeStagedGeneration,
    generationIdForProvenance,
    publishCurrentPointer,
    closedFileMap,
  } = api;
  verifyAppSourceSet(config.sidecarDir);
  runNpm(['ci'], config.sidecarDir, fail);
  const electronPackage = JSON.parse(fs.readFileSync(path.join(config.sidecarDir, 'node_modules', 'electron', 'package.json'), 'utf8'));
  if (electronPackage.version !== config.electronVersion) fail('SIDECAR_PACKAGE_ELECTRON_VERSION_MISMATCH');

  // stable root + staging（不再删除/重建 runtime 根，也不写 flat 文件）。
  createRuntimeLayout(config.runtimeDir);
  const stagingDir = path.join(config.runtimeDir, 'staging', `pending-${crypto.randomUUID()}`);
  fs.mkdirSync(stagingDir, { recursive: true });

  // Electron dist → staging；electron.exe 作为 installed 身份与 externalBin 构建输入。
  const electronDist = path.join(config.sidecarDir, 'node_modules', 'electron', 'dist');
  fs.cpSync(electronDist, stagingDir, { recursive: true, force: true, dereference: false });
  const electronExe = path.join(stagingDir, 'electron.exe');
  if (!fs.existsSync(electronExe)) fail('SIDECAR_PACKAGE_ELECTRON_RUNTIME_MISSING');
  fs.copyFileSync(electronExe, path.join(stagingDir, config.installedFile));
  fs.copyFileSync(electronExe, config.executable);
  fs.rmSync(electronExe);

  // resources/app 组装。
  fs.rmSync(path.join(stagingDir, 'resources', 'default_app.asar'), { force: true });
  const appDir = path.join(stagingDir, 'resources', 'app');
  fs.mkdirSync(appDir, { recursive: true });
  for (const relative of APP_SOURCES) {
    const source = path.join(config.sidecarDir, relative);
    if (!fs.existsSync(source)) fail('SIDECAR_PACKAGE_APP_SOURCE_MISSING');
    fs.copyFileSync(source, path.join(appDir, relative));
  }
  runNpm(['ci', '--omit=dev'], appDir, fail);
  const installedSdk = JSON.parse(fs.readFileSync(path.join(appDir, 'node_modules', 'trtc-electron-sdk', 'package.json'), 'utf8')).version;
  if (installedSdk !== config.sdkVersion) fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');

  // provenance 与 metadata 写入 staging（生成 generation.json 之前）。
  fs.writeFileSync(path.join(stagingDir, SHA_FILE), `${sha256File(path.join(stagingDir, config.installedFile))}\n`, { encoding: 'ascii' });
  const manifest = createProvenance(config, stagingDir);
  fs.writeFileSync(path.join(stagingDir, PROVENANCE_FILE), `${JSON.stringify(manifest, null, 2)}\n`, { encoding: 'utf8' });
  fs.writeFileSync(path.join(stagingDir, PROVENANCE_DIGEST_FILE), `${sha256File(path.join(stagingDir, PROVENANCE_FILE))}\n`, { encoding: 'ascii' });

  // finalize immutable generation → publish current pointer。
  const provenanceBytes = fs.readFileSync(path.join(stagingDir, PROVENANCE_FILE));
  const generation = generationIdForProvenance(provenanceBytes);
  finalizeStagedGeneration({
    runtimeDir: config.runtimeDir,
    stagingDir,
    provenanceBytes,
    expectedFiles: closedFileMap(stagingDir),
  });
  publishCurrentPointer({
    runtimeDir: config.runtimeDir,
    pointer: createCurrentPointer({ generation, manifestSha256: generation.slice(2) }),
  });

  return verifyPackage(config);
}

module.exports = { buildPackage };
