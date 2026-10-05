'use strict';

// 2026-08-21 根治：宿主环境（如 WorkBuddy 注入的 ELECTRON_RUN_AS_NODE=1 +
// NODE_OPTIONS=--use-system-ca）会让 Electron 退化为纯 Node 模式或直接拒绝启动。
// 在任何 Electron API 使用前净化 process.env；renderer/GPU 子进程继承净化后的环境。
// 必须在 require('electron') 之前执行——ELECTRON_RUN_AS_NODE 在进程引导期就被读取。
if (process.env.ELECTRON_RUN_AS_NODE) delete process.env.ELECTRON_RUN_AS_NODE;
if (process.env.NODE_OPTIONS) {
  // 只剔除 Electron 不允许的项（--use-system-ca 等 TLS 类开关），保留其余合法项
  const banned = /--use-system-ca|--use-openssl-ca|--tls-min|--tls-max/;
  const kept = process.env.NODE_OPTIONS.split(/\s+/).filter((f) => f && !banned.test(f));
  if (kept.length) process.env.NODE_OPTIONS = kept.join(' ');
  else delete process.env.NODE_OPTIONS;
}

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

// 父进程优雅停机通道（2026-10-05 Electron 43 stdin 双回归迁移，JS 侧配套）：
// 原 stdin 方案（data→"shutdown" 行 + end→EOF 兜底）在 Electron 43 主进程下
// 双回归（e2e 实锤 2026-10-05，证据 outputs/deploy-backup-20260911 与
// tmp/e43-min-app 判别探针组）：
//   1) 'end'（EOF）在 PIPE/inherit 下立即假触发（父进程持续写入也不例止；
//      同二进制 node 模式正常）→ sidecar 启动即"受控退出"秒退（生产 spawn
//      2-4s 内死，stdout 仅 2 字节）；
//   2) 'data' 事件永不触发 → shutdown 行死信 → 优雅停机失灵，退化为超时强杀。
// 故整体删除 stdin 监听，迁移为 shutdown 文件信号：父进程 spawn 时生成唯一
// 路径经 JAX_SIDECAR_SHUTDOWN_FILE 传入，停机时创建该文件；本进程轮询到文件
// 出现即走受控退出（与原 stdin shutdown 完全同路径同语义）。env 未设 = 不启用
//（开发态终端直跑兼容）。生产已全线 E43，无旧版 Electron 兼容负担。
const { watchShutdownFile } = require('./shutdown-file');
watchShutdownFile(
  process.env.JAX_SIDECAR_SHUTDOWN_FILE,
  () => exitArbiter.decide({ kind: 'controlled' })
);
app.disableHardwareAcceleration();
// 2026-09-02 GPU 沙箱修复：部分环境（本机实测复现）GPU 进程沙箱初始化失败 →
// Chromium 连试 6 次 "GPU process exited unexpectedly: exit_code=1" 后
// FATAL "GPU process isn't usable. Goodbye."，sidecar 启动 ~6 秒即死、watchdog
// 熔断。disableHardwareAcceleration() 只禁硬件加速，Chromium 仍为软件合成拉起
// GPU 进程，其沙箱失败依旧 FATAL；--disable-gpu 同样无效（GPU 进程仍被创建）。
// disable-gpu-sandbox 仅禁 GPU 进程沙箱（渲染器/工具进程沙箱全部保留），
// disable-gpu-sandbox 仅禁 GPU 进程沙箱（渲染器/工具进程沙箱全部保留），
// 对照实验：加此开关存活 >30s 且业务链完整；不加必死。最小安全面修复。
app.commandLine.appendSwitch('disable-gpu-sandbox');

// TLS 信任锚（ADR-020 A1）：app.whenReady() 前读取 NODE_EXTRA_CA_CERTS 指向的
// ca.crt，注册 certificate-error pinning——仅当 leaf 证书能被 pinned CA 验签
// （或 leaf 本身就是该 CA）时 callback(true)，否则 callback(false)。
// 缺失/不可读 = fail-closed（记 FATAL 并 fatalMain，绝不降级为无条件接受）。
//
// 2026-09-02 修正（RP-07 后续，实证 net::ERR_CERT_AUTHORITY_INVALID）：
// 旧实现依赖 certificate.issuerCert 指纹比对，但 Chromium 在验证失败时经常不提供
// issuerCert（undefined），导致 leaf!=CA 的正常 CA 签发证书永远被拒。现改为用
// Node X509Certificate.verify(publicKey) 对 pinned CA 做密码学级验签，不依赖
// Chromium 提供的链信息；验签通过 = 该证书确由 pinned CA 签发。
function loadPinnedCaCertificate() {
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
    return new X509Certificate(pem);
  } catch (error) {
    console.error(`[main] fatal invalid ca.crt at ${caPath}: ${error.message}`);
    fatalMain();
    return null;
  }
}

const pinnedCaCertificate = loadPinnedCaCertificate();

// 覆盖 Chromium 网络栈（renderer 进程 fetch，如 rtc.js/phone.js 的控制面调用）。
// 不做 URL 放行，只按 pinned CA 验签放行；验签不通过一律拒绝（fail-closed）。
app.on('certificate-error', (_event, _webContents, _url, _error, certificate, callback) => {
  if (!pinnedCaCertificate) {
    callback(false);
    return;
  }
  try {
    const leaf = new X509Certificate(certificate.data);
    const trusted =
      leaf.fingerprint256 === pinnedCaCertificate.fingerprint256 ||
      leaf.verify(pinnedCaCertificate.publicKey);
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
  // 诊断增强（2026-09-02）：renderer "Failed to fetch" 无 cause，归因困难；
  // main stdout/stderr 被 supervisor 丢弃（sidecar.rs Stdio::null），
  // 网络错误（精确 net::ERR_* 码）与 renderer console 统一落盘到诊断文件。
  // JAX_SIDECAR_LOG_DIR 与 logger.js 同源（宿主注入；未注入退回 __dirname/logs）。
  const diagFile = process.env.JAX_SIDECAR_LOG_DIR
    ? path.join(process.env.JAX_SIDECAR_LOG_DIR, 'sidecar-main-diag.log')
    : path.join(__dirname, 'logs', 'sidecar-main-diag.log');
  function diagLog(msg) {
    try {
      fs.appendFileSync(diagFile, `[${new Date().toISOString()}] ${msg}\n`);
    } catch (_) {
      /* 诊断写失败不影响业务 */
    }
  }
  win.webContents.session.webRequest.onErrorOccurred((details) => {
    diagLog(`[net] ${details.resourceType} ${details.url} -> ${details.error}`);
  });
  // Electron 35 起 console-message 回调签名变更（对象参数 + level 数字变字符串，
  // 见 electron#41613 / docs api web-contents 'console-message'）：
  //   旧: (event, level: number, message, line, sourceId)
  //   新: ({ level: 'info'|'warning'|'error'|'debug', message, lineNumber, sourceId, frame })
  // 本仓 electron 精确 pin 43.4.1，直接按新签名；warning+error 与旧 level>=2 语义对齐。
  win.webContents.on('console-message', ({ level, message, lineNumber, sourceId }) => {
    if (level === 'warning' || level === 'error') {
      diagLog(`[console:${level}] ${sourceId}:${lineNumber} ${message}`);
    }
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
