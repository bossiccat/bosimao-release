'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

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
  } = api;
  verifyAppSourceSet(config.sidecarDir);
  runNpm(['ci'], config.sidecarDir, fail);
  const electronPackage = JSON.parse(fs.readFileSync(path.join(config.sidecarDir, 'node_modules', 'electron', 'package.json'), 'utf8'));
  if (electronPackage.version !== config.electronVersion) fail('SIDECAR_PACKAGE_ELECTRON_VERSION_MISMATCH');
  fs.rmSync(config.runtimeDir, { recursive: true, force: true });
  fs.rmSync(config.executable, { force: true });
  fs.mkdirSync(config.runtimeDir, { recursive: true });
  const electronDist = path.join(config.sidecarDir, 'node_modules', 'electron', 'dist');
  fs.cpSync(electronDist, config.runtimeDir, { recursive: true, force: true, dereference: false });
  const electronExe = path.join(config.runtimeDir, 'electron.exe');
  if (!fs.existsSync(electronExe)) fail('SIDECAR_PACKAGE_ELECTRON_RUNTIME_MISSING');
  fs.copyFileSync(electronExe, config.executable);
  fs.rmSync(electronExe);
  const appDir = path.join(config.runtimeDir, 'resources', 'app');
  fs.rmSync(path.join(config.runtimeDir, 'resources', 'default_app.asar'), { force: true });
  fs.mkdirSync(appDir, { recursive: true });
  for (const relative of APP_SOURCES) {
    const source = path.join(config.sidecarDir, relative);
    if (!fs.existsSync(source)) fail('SIDECAR_PACKAGE_APP_SOURCE_MISSING');
    fs.copyFileSync(source, path.join(appDir, relative));
  }
  runNpm(['ci', '--omit=dev'], appDir, fail);
  const installedSdk = JSON.parse(fs.readFileSync(path.join(appDir, 'node_modules', 'trtc-electron-sdk', 'package.json'), 'utf8')).version;
  if (installedSdk !== config.sdkVersion) fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
  const manifest = createProvenance(config);
  fs.writeFileSync(config.hashFile, `${manifest.external_bin.sha256}\n`, { encoding: 'ascii' });
  fs.writeFileSync(config.manifestFile, `${JSON.stringify(manifest, null, 2)}\n`, { encoding: 'utf8' });
  if (config.manifestDigestFile) {
    fs.writeFileSync(config.manifestDigestFile, `${sha256File(config.manifestFile)}\n`, { encoding: 'ascii' });
  }
  return verifyPackage(config);
}

module.exports = { buildPackage };
