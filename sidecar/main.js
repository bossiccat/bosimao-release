'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { X509Certificate } = require('crypto');
const { parseArgList, validateStartup } = require('./config');
const { EXIT_CHANNEL, createExitArbiter } = require('./exit-protocol');

const exitArbiter = createExitArbiter((code) => app.exit(code));
const fatalMain = () => exitArbiter.decide({ kind: 'fatal' });

ipcMain.on(EXIT_CHANNEL, (_event, payload) => exitArbiter.decide(payload));
process.on('uncaughtException', fatalMain);
process.on('unhandledRejection', fatalMain);
process.on('SIGTERM', () => exitArbiter.decide({ kind: 'controlled' }));
app.disableHardwareAcceleration();

// TLS 信任锚（ADR-020 A1）：app.whenReady() 前读取 NODE_EXTRA_CA_CERTS 指向的
// ca.crt，计算 SHA-256 指纹，注册 certificate-error pinning——仅当证书链（leaf 或
// issuer）指纹命中该 CA 指纹时 callback(true)，否则 callback(false)。
// 缺失/不可读 = fail-closed（记 FATAL 并 fatalMain，绝不降级为无条件接受）。
function loadCaFingerprint256() {
  const caPath = process.env.NODE_EXTRA_CA_CERTS;
  if (!caPath) {
    console.error('[main] fatal NODE_EXTRA_CA_CERTS is not set; refusing to run without a pinned CA');
    fatalMain();
    return null;
  }
  let pem;
  try {
    pem = fs.readFileSync(caPath, 'utf8');
  } catch (error) {
    console.error(`[main] fatal cannot read ca.crt at ${caPath}: ${error.message}`);
    fatalMain();
    return null;
  }
  try {
    return new X509Certificate(pem).fingerprint256;
  } catch (error) {
    console.error(`[main] fatal invalid ca.crt at ${caPath}: ${error.message}`);
    fatalMain();
    return null;
  }
}

const caFingerprint256 = loadCaFingerprint256();

// 覆盖 Chromium 网络栈（renderer 进程 fetch，如 rtc.js/phone.js 的控制面调用）。
// 不做 URL 放行，只按 CA 指纹钉证书；指纹不命中一律拒绝。
app.on('certificate-error', (_event, _webContents, _url, _error, certificate, callback) => {
  if (!caFingerprint256) {
    callback(false);
    return;
  }
  try {
    const leaf = new X509Certificate(certificate.data);
    const issuer = certificate.issuerCert
      ? new X509Certificate(certificate.issuerCert.data)
      : null;
    const trusted =
      leaf.fingerprint256 === caFingerprint256 ||
      (issuer !== null && issuer.fingerprint256 === caFingerprint256);
    callback(trusted);
  } catch (_) {
    callback(false);
  }
});

app.whenReady().then(async () => {
  const businessFlags = ['--device=', '--role=', '--sign-url=', '--bridge-url=', '--wav=', '--out-wav=', '--hold='];
  const rawArgs = process.argv.slice(1).filter((arg) => businessFlags.some((flag) => arg.startsWith(flag)));
  const startupError = validateStartup(parseArgList(rawArgs), process.env);
  if (startupError) {
    console.error(`[main] fatal ${startupError}`);
    fatalMain();
    return;
  }
  const win = new BrowserWindow({
    show: false,
    width: 320,
    height: 240,
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  win.webContents.on('render-process-gone', fatalMain);
  win.webContents.on('unresponsive', fatalMain);
  try {
    await win.loadFile(path.join(__dirname, 'index.html'), { query: { args: rawArgs.join('&') } });
  } catch (_) {
    fatalMain();
  }
}).catch(fatalMain);

app.on('window-all-closed', () => {
  if (exitArbiter.verdict() === undefined) fatalMain();
});

module.exports = { fatalMain };
