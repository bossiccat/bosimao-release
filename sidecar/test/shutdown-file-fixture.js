'use strict';

// shutdown 文件信号 → 受控退出 的 Electron 端到端夹具：与 main.js 同接线
//（watchShutdownFile → exitArbiter.decide({kind:'controlled'})），供
// shutdown-file-electron.test.js 驱动。
const { app, BrowserWindow } = require('electron');
const { createExitArbiter } = require('../exit-protocol');
const { watchShutdownFile } = require('../shutdown-file');

const arbiter = createExitArbiter((code) => app.exit(code));

app.whenReady().then(() => {
  new BrowserWindow({
    show: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  // stderr 一行就绪标记：测试据此确定"存活观察窗口"起点。
  console.error('[fixture] shutdown-watch armed');
  watchShutdownFile(
    process.env.JAX_SIDECAR_SHUTDOWN_FILE,
    () => arbiter.decide({ kind: 'controlled' })
  );
});
